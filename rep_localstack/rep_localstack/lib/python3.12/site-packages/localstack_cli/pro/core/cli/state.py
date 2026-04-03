import json

import click
from localstack_cli.cli import console
from localstack_cli.cli.exceptions import CLIError
from localstack_cli.pro.core.bootstrap.pods_client import CloudPodsClient, StateService
from localstack_cli.pro.core.cli.cli import RequiresLicenseGroup, _assert_host_reachable
from localstack_cli.pro.core.cli.tree_view import TreeRenderer
from localstack_cli.utils.analytics.cli import publish_invocation
from localstack_cli.utils.collections import is_comma_delimited_list


@click.group(
    name="state",
    short_help="(Preview) Export, restore, and reset LocalStack state.",
    help="""
    (Preview) Manage and manipulate the localstack state.

    The state command group allows you to interact with LocalStack's state backend.

    Read more: https://docs.localstack.cloud/references/persistence-mechanism/#snapshot-based-persistence
    """,
    cls=RequiresLicenseGroup,
)
def state() -> None:
    pass


@state.command(
    name="reset",
    short_help="Reset the state of LocalStack services",
    help="""
    Reset the service states of the current LocalStack runtime.

    This command invokes a reset of services in the currently running LocalStack container.
    By default, all services are rest. The `services` options allows to select a subset of services
    which should be reset.

    This command tries to automatically discover the running LocalStack instance.
    If LocalStack has not been started with `localstack start` (and is not automatically discoverable),
    please set `LOCALSTACK_HOST`.
    """,
)
@click.option(
    "-s",
    "--services",
    help="Comma-delimited list of services to reset. By default, the state of all running services is reset.",
)
@publish_invocation
def reset(services: str | None) -> None:
    from localstack_cli.pro.core.bootstrap import pods_client

    if services and not is_comma_delimited_list(services):
        raise click.ClickException("Input the services as a comma-delimited list")

    service_list = [x.strip() for x in services.split(",")] if services else None
    pods_client.reset_state(services=service_list)
    console.print("LocalStack state successfully reset")


@state.command(
    name="export",
    short_help="Export the state of LocalStack services",
    help="""
    Save the current state of the LocalStack container to a file on the local disk. This file can be restored at any
    point in time using the `localstack state import` command. Please be aware that this might not be possible when
    importing the state with a different version of LocalStack.

    If you are looking for a managed solution to handle the state of your LocalStack container, please check out
    the Cloud Pods feature: https://docs.localstack.cloud/user-guide/tools/cloud-pods/.

    Use the DESTINATION argument to specify an absolute or relative path for the exported file.
    If no destination is specified, a file named `ls-state-export` will be saved in the current working directory.

    \b
    Examples:
        localstack state export my-state
        localstack state export ../parent-dir/my-state
        localstack state export /home/johndoe/my-state

    You can also specify a subset of services to export with the `--services` option.

    \b
    For example:
        localstack state export my-state --services s3,lambda

    By default, the state of all running services is exported.
    """,
)
@click.argument("destination", type=click.Path(resolve_path=True), default="ls-state-export")
@click.option(
    "-s",
    "--services",
    help="Comma-delimited list of services to reset. By default, the state of all running services is exported.",
)
@click.option(
    "-f",
    "--format",
    "format_",
    type=click.Choice(["json"]),
    help="The formatting style for the save command output.",
)
@publish_invocation
def export(destination: str, services: str | None = None, format_: str | None = None) -> None:
    _assert_host_reachable()

    client = StateService()
    try:
        services = [x.strip() for x in services.split(",") if x] if services else None
        pod_info = client.export_pod(target=destination, services=services)
        if format_ == "json":
            console.print_json(json.dumps(pod_info))
        else:
            console.print(f"LocalStack state successfully exported to: {destination} ✅")
        return
    except Exception as e:
        raise CLIError(f"Failed to export LocalStack state - {e}") from e


@state.command(
    name="import",
    short_help="Import the state of LocalStack services",
    help="""
    Load the state of LocalStack from a file into the running container.
    The SOURCE argument is the absolute or relative path to the file containing the state to import.
    This file must have been generated from a previous `localstack state export` command.
    Please be aware that it might not be possible to import a state generated from a different version of LocalStack.

    \b
    Examples:
        localstack state import my-state
        localstack state import ../parent-dir/my-state
        localstack state import /home/johndoe/my-state
    """,
)
@click.argument("source", type=click.Path(exists=True, resolve_path=True))
@publish_invocation
def import_state(source: str) -> None:
    _assert_host_reachable()

    client = StateService()
    try:
        client.import_pod(source=source, show_progress=True)
        console.print(f"LocalStack state successfully imported from {source} ✅")
    except Exception as e:
        raise CLIError(f"Failed to import LocalStack state - {e}") from e


@state.command(
    name="inspect",
    short_help="Inspect the state of LocalStack services",
    help="""
    Inspect the state of the Localstack Container.

    By default, it starts a curses interface which allows an interactive inspection of the contents of the LocalStack
    running instance.
    """,
)
@click.option(
    "-f",
    "--format",
    "format_",
    type=click.Choice(["curses", "rich", "json"]),
    default="curses",
    help="The formatting style for the inspect command output.",
)
@publish_invocation
def inspect_state(format_: str) -> None:
    _assert_host_reachable()

    client = CloudPodsClient()
    try:
        result = client.get_state_data()
    except Exception:
        raise CLIError("Error occurred while fetching the metamodel")

    # filter out too noisy/verbose services like CloudWatch
    skipped_services = ["cloudwatch"]
    for account, details in result.items():
        result[account] = {k: v for k, v in details.items() if k not in skipped_services}

    # render tree with given format
    TreeRenderer.get(format_).render_tree(result, "LocalStack State")
