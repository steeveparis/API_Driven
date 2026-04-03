import json
import logging
from urllib.parse import urlparse

import click
from localstack_cli.cli import console
from localstack_cli.cli.exceptions import CLIError
from localstack_cli.pro.core import config as pro_config
from localstack_cli.pro.core.bootstrap.auth import get_auth_cache
from localstack_cli.pro.core.bootstrap.pods.api_types import MergeStrategy
from localstack_cli.pro.core.bootstrap.pods.remotes.api import CloudPodsRemotesClient
from localstack_cli.pro.core.bootstrap.pods.remotes.configs import RemoteConfigParams
from localstack_cli.pro.core.bootstrap.pods_client import (
    CloudPodRemoteAttributes,
    CloudPodsClient,
    list_public_pods,
)
from localstack_cli.pro.core.cli.cli import (
    RequiresLicenseGroup,
    _assert_host_reachable,
)
from localstack_cli.pro.core.cli.click_utils import print_table
from localstack_cli.pro.core.cli.diff_view import print_diff
from localstack_cli.utils.analytics.cli import publish_invocation
from localstack_cli.utils.collections import is_comma_delimited_list
from localstack_cli.utils.time import timestamp

LOG = logging.getLogger(__name__)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


@click.group(
    name="pod",
    help="Manage the state of your instance via Cloud Pods.",
    context_settings={"max_content_width": 120},
    cls=RequiresLicenseGroup if not get_auth_cache().get("token") else None,
)
def pod() -> None:
    pass


@pod.group(name="remote", help="Manage cloud pod remotes")
def remote() -> None:
    pass


@remote.command(
    name="add",
    short_help="Add a remote",
    help="""
    Add a new remote for Cloud Pods.

    A remote is the place where your Cloud Pods are stored. By default, Cloud Pods are store in the LocalStack platform.
    """,
)
@click.argument("name", required=True)
@click.argument("url", required=True)
def cmd_add_remote(name: str, url: str) -> None:
    _assert_host_reachable()

    client = CloudPodsRemotesClient()
    protocol = urlparse(url).scheme
    try:
        client.create_remote(name=name, protocols=[protocol], remote_url=url)
    except Exception as e:
        raise CLIError(f"Unable to determine URL for remote '{name}': {e}") from e
    console.print(f"Successfully added remote '{name}'")


@remote.command(name="delete", short_help="Delete a remote", help="Remove a remote for Cloud Pods.")
@click.argument("name", required=True)
def cmd_delete_remote(name: str) -> None:
    _assert_host_reachable()

    client = CloudPodsRemotesClient()
    try:
        client.delete_remote(name=name)
    except Exception as e:
        raise CLIError(f"Unable to delete remote '{name}': {e}") from e
    console.print(f"Successfully deleted remote '{name}'")


@remote.command(name="list", short_help="Lists the available remotes")
@click.option(
    "-f",
    "--format",
    "format_",
    type=click.Choice(["table", "json"]),
    default="table",
    help="The formatting style for the remotes command output.",
)
def cmd_remotes(format_: str) -> None:
    _assert_host_reachable()
    client = CloudPodsRemotesClient()
    remotes = client.get_remotes()
    if not remotes:
        console.print("[yellow]No remotes[/yellow]")
        return
    if format_ == "json":
        console.print_json(json.dumps(remotes))
        return
    print_table(
        column_headers=["Remote Name", "URL"],
        columns=[[r["name"] for r in remotes], [r["url"] for r in remotes]],
    )


@pod.command(
    name="delete",
    short_help="Delete a Cloud Pod",
    help="""
    Delete a Cloud Pod registered on a remote (by default, the LocalStack platform).

    This command will remove all the versions of a Cloud Pod, and the operation is not reversible.
    """,
)
@click.argument("name")
@click.argument("remote", required=False)
@publish_invocation
def cmd_pod_delete(name: str, remote: str | None = None) -> None:
    client = CloudPodsClient()
    try:
        remote_params = RemoteConfigParams(remote_name=remote) if remote else None
        client.delete(pod_name=name, remote=remote_params)
        console.print(f"Successfully deleted Cloud Pod '{name}'")
    except Exception as e:
        raise CLIError(f"Unable to delete Cloud Pod '{name}': {e}") from e


@pod.command(
    name="save",
    short_help="Create a new Cloud Pod",
    help="""
    Save the current state of the LocalStack container in a Cloud Pod.

    A Cloud Pod can be registered and saved with different storage options, called remotes. By default, Cloud Pods
    are hosted in the LocalStack platform. However, users can decide to store their Cloud Pods in other remotes, such as
    AWS S3 buckets or ORAS registries.

    An optional message can be attached to any Cloud Pod.
    Furthermore, one could decide to export only a subset of services with the optional --services option.

    \b
    To use the LocalStack platform for storage, the desired Cloud Pod's name will suffice, e.g.:

    \b
    localstack pod save <pod_name>

    Please be aware that each following save invocation with the same name will result in a new version being created.

    \b
    To save a local copy of your state, you can use the 'localstack state export' command.
    """,
)
@click.argument("name")
@click.argument("remote", required=False)
@click.option("-m", "--message", help="Add a comment describing this Cloud Pod's version")
@click.option(
    "-s",
    "--services",
    help="Comma-delimited list of services to push in the Cloud Pod (all by default)",
)
@click.option(
    "--visibility",
    type=click.Choice(["public", "private"]),
    help="Set the visibility of the Cloud Pod [`public` or `private`]. Does not create a new version",
)
@click.option(
    "-S",
    "--secret",
    "secret",
    help="Secret for the Cloud Pod encryption. Encryption is an Enterprise only feature.",
)
@click.option(
    "-f",
    "--format",
    "format_",
    type=click.Choice(["json"]),
    help="The formatting style for the save command output.",
)
@publish_invocation
def cmd_pod_save(
    name: str | None = None,
    remote: str | None = None,
    services: str | None = None,
    message: str | None = None,
    visibility: str | None = None,
    secret: str | None = None,
    format_: str | None = None,
) -> None:
    client = CloudPodsClient(True)
    _assert_host_reachable()

    if services and not is_comma_delimited_list(services):
        raise CLIError("Input the services as a comma-delimited list")

    remote_params = RemoteConfigParams(remote_name=remote) if remote else None

    if visibility:
        try:
            client.set_remote_attributes(
                pod_name=name,
                attributes=CloudPodRemoteAttributes(is_public=True),
                remote=remote_params,
            )
        except Exception as e:
            raise CLIError(str(e))
        console.print(f"Cloud Pod {name} made {visibility}")

    service_list: list | None = [x.strip() for x in services.split(",")] if services else None

    try:
        pod_info: dict = client.save(
            pod_name=name,
            attributes=CloudPodRemoteAttributes(
                is_public=False,
                description=message,
                services=service_list,
            ),
            remote=remote_params,
            secret=secret,
        )
    except Exception as e:
        raise CLIError(f"Failed to create Cloud Pod {name} ❌ - {e}") from e
    if format_ == "json":
        console.print_json(json.dumps(pod_info))
    else:
        services = ",".join(pod_info["services"]) or "none"
        console.print(
            f"Cloud Pod `{name}` successfully created ✅\n"
            f"Version: {pod_info['version']}\n"
            f"Remote: {pod_info['remote']}\n"
            f"Services: {services}"
        )


@pod.command(
    name="load",
    help="""
    Load the state of a Cloud Pod into the application runtime.
    Users can import Cloud Pods from different remotes, with the LocalStack platform being the default one.
    Users can also load a specific version by appending a version number to the pod name after a colon
    (e.g., `localstack pod load my-pod:3`).
    If not specified, the latest version will be loaded. Use the `localstack pod versions` to list all the available
    versions.

    Loading the state of a Cloud Pod into LocalStack might cause some conflicts with the current state of the container.
    LocalStack will attempt a best-effort merging strategy between the current state and the one from the
    Cloud Pod.
    For a service X present in both the current state and the Cloud Pod, we will attempt to merge states
    across different accounts and regions. If the service X has a state for the same account and region both in the
    running container and the Cloud Pod, the latter will be used. If a service Y is present in the running container
    but not in the Cloud Pod, it will be left untouched.
    This is the default merge strategy which is activated by either the `--strategy account-region-merge` option or
    by omitting the `--strategy` option at all.

    In addition to the default one, LocalStack provides two more strategies:

    \b
    - overwrite, in which the state of LocalStack is completely reset before loading the state from the Cloud Pod.
    This strategy is activated with the `--strategy overwrite` option .
    - service-merge: in which LocalStack merges the state of a service under the same account and region when
    there is no resource overlap. In such a case, the loaded resources are preferred.
    This option is activated with the `--strategy service-merge` option.

    To load a local copy of a LocalStack state, you can use the 'localstack state import' command.
    """,
)
@click.argument("name")
@click.argument("remote", required=False)
@click.option(
    "-s",
    "--secret",
    "secret",
    help="Secret for the Cloud Pod encryption. Encryption is an Enterprise only feature.",
)
@click.option(
    "--strategy",
    type=click.Choice([e.value for e in MergeStrategy]),  # noqa
    default=pro_config.MERGE_STRATEGY,
    help="The merge strategy to adopt when loading the Cloud Pod.",
)
@click.option(
    "--yes",
    "-y",
    help="Automatic yes to prompts. Assume a positive answer to all prompts and run non-interactively.",
    is_flag=True,
    default=False,
)
@click.option(
    "--dry-run",
    help="Checks the resources added or modified in the application runtime by loading a Cloud Pod.",
    is_flag=True,
    default=False,
)
@publish_invocation
def cmd_pod_load(
    name: str | None = None,
    remote: str | None = None,
    strategy: str | None = None,
    yes: bool = False,
    secret: str | None = None,
    dry_run: bool = False,
) -> None:
    client = CloudPodsClient(True)

    _assert_host_reachable()
    pod_name, version = get_pod_name_and_version(name)

    if dry_run:
        try:
            diffing_ops = client.diff(pod_name=pod_name, remote=remote, version=version)
            print_diff(diffing_ops)
            return
        except Exception as e:
            raise CLIError(f"Failed to dry-run the load operation: {e}") from e

    try:
        remote_params = RemoteConfigParams(remote_name=remote) if remote else None
        client.load(
            pod_name=pod_name,
            remote=remote_params,
            version=version,
            merge_strategy=strategy,
            ignore_version_mismatches=yes,
            secret=secret,
        )
    except Exception as e:
        raise CLIError(f"Failed to load Cloud Pod {name}: {e}") from e
    print(f"Cloud Pod {name} successfully loaded")


def get_pod_name_and_version(pod_name: str) -> tuple[str, int | None]:
    """
    Retrieve the name and the version number from the pod name. We have a Docker-like convention to specify the version
    numbers i.e., <pod_name>:<version>. If we can't parse a version, we assume the last available version.
    """

    if ":" not in pod_name:
        return pod_name, None

    _pod_name, _, version = pod_name.rpartition(":")
    if version.isdigit():
        return _pod_name, int(version)
    return pod_name, None


@pod.command(
    name="list",
    short_help="List all available Cloud Pods",
    help="""
    List all the Cloud Pods available for a single user, or for an entire organization, if the user is part of one.

    With the --public flag, it lists the all the available public Cloud Pods. A public Cloud Pod is available across
    the boundary of a user and/or organization. In other words, any public Cloud Pod can be injected by any other
    user holding a LocalStack Pro (or above) license.
    """,
)
@click.argument("remote", required=False)
@click.option(
    "--public", "-p", help="List all the available public Cloud Pods", is_flag=True, default=False
)
@click.option(
    "--mine",
    "-m",
    help="List only the Cloud Pods created by the current user",
    is_flag=True,
    default=False,
)
@click.option(
    "-f",
    "--format",
    "format_",
    type=click.Choice(["table", "json"]),
    default="table",
    help="The formatting style for the list pods command output.",
)
@publish_invocation
def cmd_pod_list_pods(
    remote: str | None = None, public: bool = False, mine: bool = False, format_: str = None
) -> None:
    client = CloudPodsClient()

    _assert_host_reachable()

    if public:
        public_pods = list_public_pods()
        print_table(column_headers=["Cloud Pod"], columns=[public_pods])
        return

    remote_params = RemoteConfigParams(remote_name=remote) if remote else None
    pods: list[dict] = client.list(remote=remote_params, creator="me" if mine else None)
    if not pods:
        console.print("[yellow]No pods available[/yellow]")

    if format_ == "json":
        console.print_json(json.dumps(pods))
        return
    print_table(
        column_headers=["Name", "Max Version", "Last Change"],
        columns=[
            [pod["pod_name"] for pod in pods],
            [str(pod["max_version"]) for pod in pods],
            [
                timestamp(pod["last_change"], format=DATE_FORMAT)
                if pod.get("last_change")
                else "n/a"
                for pod in pods
            ],
        ],
    )


@pod.command(
    name="versions",
    help="""
    List all available versions for a Cloud Pod

    This command lists the versions available for a Cloud Pod.
    Each invocation of the save command is going to create a new version for a named Cloud Pod, if a Pod with
    such name already does exist in the LocalStack platform.
    """,
)
@click.argument("name")
@click.option(
    "-f",
    "--format",
    "format_",
    type=click.Choice(["table", "json"]),
    default="table",
    help="The formatting style for the version command output.",
)
@publish_invocation
def cmd_pod_versions(name: str, format_: str) -> None:
    client = CloudPodsClient()
    _assert_host_reachable()

    try:
        versions: list[dict] = client.get_versions(pod_name=name)
        versions = [v for v in versions if not v.get("deleted")]
    except Exception as e:
        raise CLIError(str(e)) from e
    if not versions:
        console.print("[yellow]No versions available[/yellow]")

    if format_ == "json":
        console.print_json(json.dumps(versions))
        return
    print_table(
        column_headers=[
            "Version",
            "Creation Date",
            "LocalStack Version",
            "Services",
            "Description",
        ],
        columns=[
            [str(r["version"]) for r in versions],
            [timestamp(r["created_at"], format=DATE_FORMAT) for r in versions],
            [r["localstack_version"] for r in versions],
            [",".join(r["services"] or []) for r in versions],
            [r["description"] for r in versions],
        ],
    )
