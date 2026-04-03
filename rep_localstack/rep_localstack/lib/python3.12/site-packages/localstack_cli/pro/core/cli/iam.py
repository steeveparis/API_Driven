import json
import subprocess
from typing import Any

import click
import requests
from click import ClickException
from localstack_cli import config
from localstack_cli.cli import console
from localstack_cli.pro.core.cli.aws import aws
from localstack_cli.pro.core.cli.cli import RequiresPlatformLicenseGroup
from localstack_cli.utils.analytics.cli import publish_invocation
from localstack_cli.utils.platform import is_windows
from localstack_cli.utils.strings import to_str


def _print_plain(generated_policy: dict[str, Any]):
    console.print(
        f'Attached to {generated_policy["policy_type"]}: "{generated_policy["resource"]}"'
    )
    console.line()
    console.print("Policy: ")
    console.print_json(data=generated_policy["policy_document"])
    console.line()
    console.rule()
    console.line()


def print_generated_policy_plain(generated_policy: bytes):
    generated_policy = json.loads(to_str(generated_policy))
    _print_plain(generated_policy)


def print_generated_policy_json(generated_policy: bytes):
    console.print(to_str(generated_policy), highlight=False)


def get_iam_endpoint():
    edge_url = config.external_service_url()
    return f"{edge_url}/_aws/iam"


class IAMCliGroup(RequiresPlatformLicenseGroup):
    name = "iam-stream"
    # TODO this should be the plan name, but those should not necessarily be hardcoded. Maybe
    # change the wording in the error message for RequiresPlatformLicenseGroup altogether
    tier = "higher"


@aws.group(
    name="iam",
    short_help="(Preview) Access LocalStack IAM features",
    help="""
    Access LocalStack IAM features.

    This command provides tools to make it easier to write IAM policies for your cloud application.
    """,
    cls=IAMCliGroup,
)
def iam() -> None:
    pass


@iam.command(
    name="stream",
    short_help="Stream policies for all requests enforced on LocalStack",
    help="""
    Live stream of policies as requests are coming into LocalStack.

    This command generates a live stream of policies and the principals or resources they should be attached to.

    For every request, it will print the principal or resource the policy should be attached to first.
    (will be a service resource if it is a resource based policy, an IAM principal otherwise)
    After that the recommended policy will be printed.
    """,
)
@click.option(
    "-f",
    "--format",
    "format_",
    type=click.Choice(["plain", "json"]),
    default="plain",
    help="The formatting style for the command output. Use plain if it should be human readable, and json to get a "
    "newline-separated list of json documents.",
)
@publish_invocation
def cmd_iam_stream(format_: str) -> None:
    try:
        with requests.get(f"{get_iam_endpoint()}/policies/stream", stream=True) as response:
            empty_policy_hint = "Please perform request against LocalStack to start seeing policies here. Waiting for policies..."
            console.print(empty_policy_hint)
            for generated_policy in response.iter_lines():
                if format_ == "plain":
                    print_generated_policy_plain(generated_policy)
                elif format_ == "json":
                    print_generated_policy_json(generated_policy)
    except requests.ConnectionError:
        raise ClickException(
            "Unable to connect to the LocalStack Pro instance.\n"
            "Please make sure you have an instance up and running!"
        )
    except json.JSONDecodeError:
        raise ClickException(
            "Invalid response from the LocalStack instance.\n"
            "Please update your LocalStack instance!"
        )
    except Exception as e:
        raise ClickException(f"Error while streaming Policies: {e}")


def clear_terminal():
    if is_windows():
        subprocess.run("cls")
    else:
        subprocess.run("clear")


@iam.command(
    name="summary",
    short_help="Summary of policies for all requests enforced on LocalStack",
    help="""
    Live view of all policies required for running your current stack on LocalStack

    This command generates a live view of policies and the principals or resources they should be attached to.

    This will clear your terminal.
    The policies will update if a requests requires additional permissions for the principal making it.
    """,
)
@click.option("-o", "--output", help="File location to write the json output to.")
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    default=False,
    help="Whether to continuously monitor the summary changes.",
)
@publish_invocation
def cmd_iam_summary(output: str | None, follow: bool) -> None:
    policy_set = None
    try:
        if follow:
            with requests.get(
                f"{get_iam_endpoint()}/policies/summary?stream=1", stream=True
            ) as response:
                for policy_set in response.iter_lines():
                    clear_terminal()
                    policy_set = json.loads(to_str(policy_set))
                    if not policy_set:
                        console.print(
                            "Please perform request against LocalStack to start seeing policies here. Waiting for policies..."
                        )
                    for policy in policy_set:
                        _print_plain(policy)
        else:
            response = requests.get(f"{get_iam_endpoint()}/policies/summary")
            policy_set = response.json()
            if not policy_set:
                console.print(
                    "No policies available yet. Please perform requests against LocalStack to get generated policies."
                )
            for policy in policy_set:
                _print_plain(policy)

    except requests.ConnectionError:
        raise ClickException(
            "Unable to connect to the LocalStack Pro instance.\n"
            "Please make sure you have an instance up and running!"
        )
    except json.JSONDecodeError:
        raise ClickException(
            "Invalid response from the LocalStack instance.\n"
            "Please update your LocalStack instance!"
        )
    except Exception as e:
        raise ClickException(f"Error while streaming Policies: {e}")
    finally:
        if policy_set and output:
            with open(output, mode="w") as f:
                json.dump(policy_set, fp=f)
