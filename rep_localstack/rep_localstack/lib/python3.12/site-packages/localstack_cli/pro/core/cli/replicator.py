import json
import os
import re
import subprocess as sp
import sys
import time
from collections.abc import Mapping
from configparser import ConfigParser
from pathlib import Path
from typing import TypedDict

import click
import requests

from localstack_cli import config
from localstack_cli.cli import console
from localstack_cli.cli.exceptions import CLIError

from .cli import RequiresPlatformLicenseGroup, _assert_host_reachable

AWS_CONFIG_ENV_VARS = {
    "aws_access_key_id": "{}_ACCESS_KEY_ID",
    "aws_secret_access_key": "{}_SECRET_ACCESS_KEY",
    "aws_session_token": "{}_SESSION_TOKEN",
    "region_name": "{}_DEFAULT_REGION",
    "endpoint_url": "{}_ENDPOINT_URL",
    "profile_name": "{}_PROFILE",
}

PREVIEW_BANNER = """
*** Preview Feature ***

This feature is currently in preview mode in our Teams offering and it's availability may change in future releases.
"""

REPLICATOR_HELP = (
    PREVIEW_BANNER
    + """

The replicator command group allows you to replicate AWS resources into LocalStack.
"""
)


def run_check_output(cmd) -> str:
    return sp.check_output(cmd, stderr=sp.PIPE, env=os.environ).decode("utf-8").strip()


class ProfileLoadError(RuntimeError):
    def __init__(self, profile_name: str):
        super().__init__(f"Could not find profile '{profile_name}'")


class ReplicatorCliGroup(RequiresPlatformLicenseGroup):
    name = "replicator"
    tier = "Ultimate"

    def invoke(self, ctx: click.Context):
        print(PREVIEW_BANNER, file=sys.stderr)
        super().invoke(ctx)


class AWSConfig(TypedDict, total=False):
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str | None
    region_name: str
    endpoint_url: str | None
    profile_name: str


def get_aws_env_config(prefix: str) -> AWSConfig:
    aws_config = {
        key: os.getenv(value.format(prefix)) for key, value in AWS_CONFIG_ENV_VARS.items()
    }
    return AWSConfig(**{k: v for k, v in aws_config.items() if v})


def get_config_from_profile(profile_name: str, profile_dir: Path | None = None) -> AWSConfig:
    profile_dir = profile_dir or Path.home() / ".aws"

    def load_config_for_profile(path: Path, profile_prefix: str = "") -> Mapping:
        parser = ConfigParser()
        parser.read(path)
        try:
            return parser[f"{profile_prefix}{profile_name}"]
        except KeyError:
            raise ProfileLoadError(profile_name)

    config = load_config_for_profile(profile_dir / "config", profile_prefix="profile ")
    credentials = load_config_for_profile(profile_dir / "credentials")

    return AWSConfig(
        aws_access_key_id=credentials["aws_access_key_id"],
        aws_secret_access_key=credentials["aws_secret_access_key"],
        region_name=config["region"],
        profile_name=profile_name,
    )


def get_awscli_config() -> AWSConfig | None:
    try:
        # get credentials values
        cmd = ["aws", "configure", "export-credentials"]
        credentials = json.loads(run_check_output(cmd))
    except sp.CalledProcessError as exc:
        if b"AWS CLI version 2" in exc.stderr:
            print(
                "Warning: awscli v1 installed. Please use v2 for auto detection of credentials",
                file=sys.stderr,
            )
        return None

    try:
        # try to get the endpoint url
        cmd = ["aws", "configure", "get", "endpoint_url"]
        endpoint_url = run_check_output(cmd)
    except sp.CalledProcessError:
        # If there are no endpoint configured an exception is raised we do a last
        # check in the environment to find the endpoint url
        endpoint_url = os.getenv("AWS_ENDPOINT_URL")

    try:
        # try to get the region from configure
        cmd = ["aws", "configure", "get", "region"]
        region_name = run_check_output(cmd)
    except sp.CalledProcessError:
        # If there are no default configured an exception is raised we do a last
        # check in the environment to find the region
        region_name = os.getenv("AWS_DEFAULT_REGION")

    if not region_name:
        try:
            # older awscli versions do not return the region to the command `aws configure get region`.
            # We need to rely on the configure list in this case
            cmd = ["aws", "configure", "list"]
            for line in run_check_output(cmd).splitlines():
                if "region" not in line:
                    continue
                # aws changed the format of configure and added `:` in delimiters
                words = re.split(r"[:\s]+", line)
                try:
                    region_name = words[1]
                    break
                except IndexError:
                    return None
        except (sp.CalledProcessError, FileNotFoundError):
            return None

    return AWSConfig(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials.get("SessionToken"),
        region_name=region_name,
        endpoint_url=endpoint_url,
    )


def get_source_config(profile_dir: Path | None = None) -> AWSConfig:
    awscli_source_config = get_awscli_config()
    if awscli_source_config:
        print("Configured credentials from the AWS CLI", file=sys.stderr)
        return awscli_source_config

    source_config = get_aws_env_config("AWS")
    profile_name = source_config.get("profile_name")

    if source_config.get("aws_access_key_id") and not source_config.get("aws_secret_access_key"):
        raise CLIError(
            "Unable to retrieve credentials: Partial credentials found in env."
            " Need both 'AWS_ACCESS_KEY_ID' and 'AWS_SECRET_ACCESS_KEY'"
        )

    if profile_name:
        config_file_source_config = get_config_from_profile(
            profile_name=profile_name, profile_dir=profile_dir
        )
        if source_config.get("aws_secret_access_key"):
            # before merging, if the secret access key is in the env vars we need to ensure the session token from the
            # config file isn't carried along
            config_file_source_config.pop("aws_session_token", None)
        config_file_source_config.update(source_config)
        source_config = config_file_source_config

    errors = []

    if not source_config.get("region_name"):
        errors.append("'AWS_DEFAULT_REGION' must bet set in environment or in profile.")
    if not source_config.get("aws_access_key_id"):
        errors.append("'AWS_ACCESS_KEY_ID' must bet set in environment or in profile.")
    if not source_config.get("aws_secret_access_key"):
        errors.append("'AWS_SECRET_ACCESS_KEY' must bet set in environment or in profile.")

    if errors:
        raise CLIError("\n".join(errors))

    return source_config


def get_target_config(access_key: str = "", region_name: str = "") -> AWSConfig:
    target_config = get_aws_env_config("TARGET")
    if access_key:
        target_config["aws_access_key_id"] = access_key
    if region_name:
        target_config["region_name"] = region_name

    return target_config


def get_replicator_url():
    _assert_host_reachable()
    return f"{config.external_service_url()}/_localstack/replicator"


@click.group(
    name="replicator",
    short_help="(Preview) Start a replication job or check its status",
    help=REPLICATOR_HELP,
    cls=ReplicatorCliGroup,
)
def replicator() -> None:
    pass


def validate_start_command(
    replication_type: str,
    resource_arn: str | None = None,
    resource_type: str | None = None,
    resource_identifier: str | None = None,
) -> None:
    if replication_type == "SINGLE_RESOURCE":
        if not (resource_arn or resource_type):
            raise CLIError("You must specify either --resource-arn or --resource_type")

        if resource_arn and resource_type:
            raise CLIError("You must specify either --resource-arn or --resource_type")

        if resource_type and not resource_identifier:
            raise CLIError("You must specify --resource-id when using --resource-type")

        if resource_arn and not resource_arn.startswith("arn:aws:"):
            raise CLIError("--resource-arn must start with arn:aws:")

    return None


@replicator.command(
    name="start",
    short_help="Replicate an AWS resource",
    help="""
Starts a job to replicate an AWS resource into localstack.
You must have credentials with sufficient read access to the resource trying to replicate.
At the moment only environment variables are recognized.
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_DEFAULT_REGION` must be set. `AWS_ENDPOINT_URL` and `AWS_SESSION_TOKEN` are optional.
""",
)
@click.option(
    "--replication-type",
    type=click.Choice(["SINGLE_RESOURCE", "BATCH"]),
    default="SINGLE_RESOURCE",
    show_default=True,
    help="Type of replication job: SINGLE_RESOURCE, BATCH",
)
@click.option(
    "--explore-strategy",
    type=click.Choice(["SIMPLE", "TREE"]),
    default="SIMPLE",
    show_default=True,
    help="How we explore the resource tree. SIMPLE only replicates the resource requested.",
)
@click.option(
    "--resource-arn",
    help="ARN of the resource to recreate. Optional for SINGLE_RESOURCE replication",
)
@click.option(
    "--resource-type",
    help="CloudControl type of the resource to recreate. Optional for SINGLE_RESOURCE replication",
)
@click.option(
    "--resource-identifier",
    help="CloudControl identifier of the resource to recreate. Mandatory if --resource-type is used",
)
@click.option(
    "--target-account-id",
    # TODO add link to the docs
    help="Localstack account ID where the resources will be replicated. Defaults to 000000000000. See <docs> to enable same account replication",
)
@click.option(
    "--target-region-name",
    # TODO add link to the docs
    help="Localstack region where the resources will be replicated. Only provide if different than source AWS account.",
)
@click.option("--delay", help="Delay for the MOCK replication work")
def start(
    replication_type: str,
    explore_strategy: str,
    resource_arn: str | None = None,
    resource_type: str | None = None,
    resource_identifier: str | None = None,
    delay: str | None = None,
    target_account_id: str | None = None,
    target_region_name: str | None = None,
) -> None:
    validate_start_command(replication_type, resource_arn, resource_type, resource_identifier)

    source_config = get_source_config()
    # TODO Do we want to keep those in the cli? there may be some Most Common Settings
    #  that we can allow from the cli for simplicity, and the rest through env var?
    target_config = get_target_config(access_key=target_account_id, region_name=target_region_name)

    replication_config = {}
    if resource_arn:
        replication_config["resource_arn"] = resource_arn
    if resource_type:
        replication_config["resource_type"] = resource_type
    if resource_identifier:
        replication_config["resource_identifier"] = resource_identifier

    if replication_type == "MOCK":
        replication_config["delay"] = float(delay) if delay else 1

    replicator_url = f"{get_replicator_url()}/jobs"

    payload = {
        "replication_type": replication_type,
        "explore_strategy": explore_strategy,
        "replication_job_config": replication_config,
        "source_aws_config": source_config,
        "target_aws_config": target_config,
    }
    response = requests.post(replicator_url, json=payload)

    if response.status_code == 200:
        console.print_json(json=response.text)
    else:
        raise CLIError(f"Failed to create replication job: {response.status_code}, {response.text}")


@replicator.command(
    name="status",
    short_help="Check replication status",
    help="""
Check the status of a replication job using its Job ID.
Use the --follow flag to continuously check the status until the job is completed.
""",
)
@click.argument("job_id")
@click.option("--follow", is_flag=True, help="Follow the status until completed")
@click.option("--delay", help="Delay between calls", default=5, type=int)
def status(job_id, follow: bool, delay: int) -> None:
    url = f"{get_replicator_url()}/jobs/{job_id}"
    while True:
        response = requests.get(url)

        if response.status_code == 200:
            job = response.json()
            console.print_json(data=job)
            job_state = job.get("state")
            if job_state == "ERROR":
                raise CLIError(job.get("error_message"))
            elif job_state == "SUCCEEDED":
                return
        else:
            raise CLIError(f"Failed to replicate resource: {response.status_code}, {response.text}")

        if not follow:
            return
        time.sleep(float(delay))


@replicator.command(name="resources", short_help="List supported resources")
def resources():
    url = f"{get_replicator_url()}/resources"
    response = requests.get(url)
    if response.status_code != 200:
        raise CLIError(f"Failed to get list of resources: {response.status_code}, {response.text}")

    console.print_json(json=response.text)
