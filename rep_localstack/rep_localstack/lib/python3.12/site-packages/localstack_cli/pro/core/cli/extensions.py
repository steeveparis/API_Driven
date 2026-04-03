from __future__ import annotations

import json
import os
from collections.abc import Iterable

import click
from click import ClickException
from localstack_cli import constants
from localstack_cli.cli import console
from localstack_cli.utils.analytics.cli import publish_invocation
from localstack_cli.utils.container_utils.container_client import (
    BindMount,
    ContainerConfiguration,
    ContainerConfigurator,
    ContainerException,
)
from rich.status import Status
from rich.table import Table

from .cli import RequiresLicenseGroup

_PYTHON_IN_CONTAINER = "/opt/code/localstack/.venv/bin/python"
"""Path inside the container pointing to the Python executable."""


@click.group(
    name="extensions",
    short_help="(Preview) Manage LocalStack extensions",
    help="""
    (Preview) Manage LocalStack extensions.

    LocalStack Extensions allow developers to extend and customize LocalStack.
    The feature and the API are currently in a preview stage and may be subject to change.

    If you are using LocalStack extensions with docker-compose, you can use the CLI by pointing the
    `LOCALSTACK_VOLUME_DIR=` variable to localstack volume directory on your host. By default, the volume
    on your host is located in `~/.cache/localstack` on Linux, and `~/Library/Caches` on Mac.

    Visit https://docs.localstack.cloud/references/localstack-extensions/ for more information on LocalStack
    Extensions.
    """,
    cls=RequiresLicenseGroup,
)
@click.pass_context
@click.option("-v", "--verbose", is_flag=True, default=False, help="Print more output")
def extensions(ctx: click.Context, verbose: bool) -> None:
    ctx.ensure_object(dict)
    ctx.obj["VERBOSE"] = verbose


@extensions.command(
    "init",
    help="""
    Initialize the LocalStack extensions environment.

    The environment variable `LOCALSTACK_VOLUME_DIR` currently defaults to ~/.cache/localstack, where the
    extension environment will be installed into ./lib/extensions/
    """,
)
@publish_invocation
def cmd_extensions_init() -> None:
    for line in _stream_localstack_container_command(
        [_PYTHON_IN_CONTAINER, "-m", "localstack.pro.core.bootstrap.extensions", "init"]
    ):
        console.log(line)


@extensions.command(
    "install",
    help="""
    Install a LocalStack extension.

    This command installs a LocalStack extension, where the name can be any valid pip dependency
    identifier. Additionally, we support the installation of distribution files from disk, which you can
    indicate by a ``file://`` prefix in the name

    \b
    Example invocations:
        localstack extensions install localstack-extension-stripe
        localstack extensions install "git+https://github.com/localstack/localstack-stripe.git#egg=localstack-stripe"
        localstack extensions install file://./dist/localstack-extension-hello-world-0.1.0.tar.gz
        localstack extensions install file://.  # assumes the current directory is a source distribution
    """,
)
@click.pass_context
@click.argument("name", required=True)
@publish_invocation
def cmd_extensions_install(ctx: click.Context, name: str) -> None:
    configurators = []
    if name.startswith("file://"):
        file_path = name[7:]
        if not os.path.exists(file_path):
            raise ClickException(f"No such file {file_path}")
        file_path = os.path.abspath(file_path)

        # map the host path to a container path
        # not using os.path here because it needs to be a unix path in the container
        name = f"/tmp/{os.path.basename(file_path)}"
        configurators.append(lambda cfg: cfg.volumes.add(BindMount(file_path, name)))

    status = console.status("Initializing")

    with status:
        _ensure_venv_initialized()

        stream = _stream_localstack_container_command(
            [
                _PYTHON_IN_CONTAINER,
                "-m",
                "localstack.pro.core.bootstrap.extensions",
                "install",
                name,
            ],
            configurators,
        )
        _process_extensions_event_stream(stream, status, ctx.obj["VERBOSE"])


@extensions.command(
    "uninstall",
    help="""
    Remove a LocalStack extension.

    This command removes a previously installed LocalStack extension, where the name can be any valid package name.

    \b
    Example invocations:
        localstack extensions uninstall localstack-extension-stripe
    """,
)
@click.pass_context
@click.argument("name", required=True)
@publish_invocation
def cmd_extensions_uninstall(ctx: click.Context, name: str) -> None:
    status = console.status("Initializing")

    with status:
        _ensure_venv_initialized()

        stream = _stream_localstack_container_command(
            [
                _PYTHON_IN_CONTAINER,
                "-m",
                "localstack.pro.core.bootstrap.extensions",
                "uninstall",
                name,
            ]
        )
        _process_extensions_event_stream(stream, status, ctx.obj["VERBOSE"])


@extensions.command(
    "list",
    help="""
    List installed extension.

    The environment variable `LOCALSTACK_VOLUME_DIR` currently defaults to ~/.cache/localstack, where the
    extension environment will be installed into ./lib/extensions/
    """,
)
@publish_invocation
def cmd_extensions_list() -> None:
    cmd = [_PYTHON_IN_CONTAINER, "-m", "localstack.pro.core.bootstrap.extensions", "list"]

    status = console.status("Querying ...")
    status.start()
    try:
        lines = _stream_localstack_container_command(cmd)

        extensions_ = []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
                extensions_.append(doc)
            except json.JSONDecodeError:
                # skip lines that cannot be parsed as JSON
                pass

        status.stop()

        console.print(_extension_metadata_table(extensions_))
    finally:
        status.stop()


def _ensure_venv_initialized():
    try:
        _assert_venv_initialized()
    except ClickException:
        for line in _stream_localstack_container_command(
            [_PYTHON_IN_CONTAINER, "-m", "localstack.pro.core.bootstrap.extensions", "init"]
        ):
            console.log(line)

    _assert_venv_initialized()


def _assert_venv_initialized() -> None:
    from localstack_cli import config
    from localstack_cli.pro.core.bootstrap.extensions import repository

    path = os.path.join(config.VOLUME_DIR, "lib", repository.VENV_DIRECTORY)

    if not os.path.exists(path):
        raise ClickException(
            "extensions dir not initialized, please run `localstack extensions init` "
            "first or check if `LOCALSTACK_VOLUME_DIR` is set correctly"
        )


def _process_extensions_event_stream(stream: Iterable[str | dict], status: Status, verbose: bool):
    """
    The extension event stream is
    :param stream:
    :param status:
    :param verbose:
    :return:
    """
    extensions_ = []
    exception = False

    for line in stream:
        try:
            if isinstance(line, dict):
                event = line
            else:
                event = json.loads(line)
        except json.JSONDecodeError:
            console.log("couldn't parse container response", line)
            continue

        if "event" not in event:
            console.log("couldn't parse container response", line)
        elif event["event"] == "status":
            status.update(event["message"])
        elif event["event"] == "log":
            console.log(event["message"])
        elif event["event"] == "error":
            console.log("Error:", event["message"])
        elif event["event"] == "pip":
            if verbose:
                console.log(event["message"])
        elif event["event"] == "extension":
            extensions_.append(event["extra"])
        elif event["event"] == "exception":
            console.log(event["message"])
            exception = True
            if verbose and "extra" in event:
                console.log(event["extra"].get("traceback"))
        else:
            console.log("unknown event type in container response", line)

    if exception and not verbose:
        console.log(
            "An error occurred while processing the extension. You can run the the extensions command again "
            "with the --verbose flag to get more information about the error."
        )

    # this is only used for the install command to list the extensions that were installed
    extensions_ = [e for e in extensions_ if e]
    if extensions_:
        console.print(_extension_metadata_table(extensions_))


def _extension_metadata_table(extensions_: list[dict]) -> Table:
    """Creates a rich.Table from the given list of extension metadata objects."""
    t = Table()
    t.add_column("Name")
    t.add_column("Summary")
    t.add_column("Version")
    t.add_column("Author")
    t.add_column("Plugin name")

    for e in extensions_:
        if not (author := e["distribution"].get("author")):
            author = e["distribution"].get("author_email")

        t.add_row(
            e["distribution"]["name"],
            e["distribution"]["summary"],
            e["distribution"]["version"],
            author,
            e["name"],
        )

    return t


@extensions.group("dev")
def dev() -> None:
    """
    Developer tools for developing LocalStack extensions.
    """
    pass


@dev.command(
    "new",
    help="""
    Create a new LocalStack extension from the official extension template.

    \b
    The templating relies on cookiecutter, which you can install with
        pip install cookiecutter

    \b
    The template can be found at
    https://github.com/localstack/localstack-extensions/tree/main/template.

    The new extension will be created in your current working directory under the <project_slug> parameter.
    """,
)
@click.option(
    "--template",
    default="basic",
    help="Specify the template to use from "
    "https://github.com/localstack/localstack-extensions/tree/main/templates",
)
@publish_invocation
def cmd_dev_new(template: str) -> None:
    try:
        from cookiecutter.main import cookiecutter
    except ImportError:
        msg = "this command requires the cookiecutter CLI, please run:\npip install cookiecutter"
        raise ClickException(msg)

    cookiecutter(
        "https://github.com/localstack/localstack-extensions", directory=f"templates/{template}"
    )


@dev.command(
    "enable",
    help="""
    Enables an extension on the host for developer mode.

    Extensions for which dev mode is enabled will be mounted into the LocalStack container the next time it runs.

        PATH: the path to the extension (can be relative).
    """,
)
@click.argument("path", type=click.Path(exists=True))
@publish_invocation
def cmd_dev_enable(path: str) -> None:
    from localstack_cli import config
    from localstack_cli.utils.json import FileMappedDocument

    path = os.path.abspath(path)

    config = FileMappedDocument(os.path.join(config.CONFIG_DIR, "extensions-dev.json"))

    if "extensions" not in config:
        config["extensions"] = []

    for ext in config["extensions"]:
        if ext["host_path"] == path:
            click.echo(f"{path} already enabled")
            return

    config["extensions"].append({"host_path": path})
    config.save()
    click.echo(f"{path} enabled")


@dev.command(
    "disable",
    help="""
    Disables an extension on the host for developer mode.

    Extensions for which dev mode is enabled will be mounted into the LocalStack container the next time it runs.

        PATH: the path to the extension (can be relative).
    """,
)
@click.argument("path", type=click.Path(exists=False))
@publish_invocation
def cmd_dev_disable(path: str) -> None:
    from localstack_cli import config
    from localstack_cli.utils.json import FileMappedDocument

    path = os.path.abspath(path)

    config = FileMappedDocument(os.path.join(config.CONFIG_DIR, "extensions-dev.json"))

    if "extensions" not in config:
        config["extensions"] = []

    len_before = len(config["extensions"])
    config["extensions"] = [ext for ext in config["extensions"] if ext["host_path"] != path]
    len_after = len(config["extensions"])

    if len_before == len_after:
        click.echo(f"{path} not enabled")
        return

    config.save()
    click.echo(f"{path} disabled")


@dev.command(
    "list",
    help="""
    List LocalStack extensions for which dev mode is enabled.
    """,
)
def cmd_dev_list() -> None:
    from localstack_cli import config
    from localstack_cli.utils.json import FileMappedDocument

    config = FileMappedDocument(os.path.join(config.CONFIG_DIR, "extensions-dev.json"))

    if "extensions" not in config:
        return

    for ext in config["extensions"]:
        click.echo(ext["host_path"])


def _stream_localstack_container_command(
    cmd: list[str],
    additional_configurators: Iterable[ContainerConfigurator] = (),
):
    """
    Convenience function to run a command inside a fresh localstack pro container and stream its output as
    a generator.

    :param cmd: the command to run inside the localstack container.
    :param additional_configurators: additional container configurators
    :return: a generator that yields each line from stdout from the container command
    """
    from localstack_cli import config
    from localstack_cli.pro.core.bootstrap import licensingv2
    from localstack_cli.utils import docker_utils
    from localstack_cli.utils.bootstrap import (
        Container,
        ContainerConfigurators,
    )

    # either use the content of the IMAGE_NAME env var, or default to the pro image
    # (extensions are not supported in community)
    image_name = os.environ.get("IMAGE_NAME")
    if not image_name:
        image_name = constants.DOCKER_IMAGE_NAME_PRO

    container = Container(
        ContainerConfiguration(image_name=image_name, remove=False),
        docker_client=docker_utils.DOCKER_CLIENT,
    )

    # recipe for extensions command container
    configurators = [
        ContainerConfigurators.env_vars(
            {
                "DEBUG": "0",
            }
        ),
        ContainerConfigurators.custom_command(cmd),
        ContainerConfigurators.mount_localstack_volume(config.VOLUME_DIR),
        licensingv2.configure_container_licensing,
        *additional_configurators,
    ]
    # if you want to test local changes to the backend, you can add this volume mount:
    # configurators.append(
    #     ContainerConfigurators.volume(
    #         VolumeBind(
    #             "/path/to/localstack-pro/localstack-pro-core/localstack/pro/core/bootstrap",
    #             "/opt/code/localstack/.venv/lib/python3.11/site-packages/localstack/pro/core/bootstrap"
    #         )
    #     )
    # )
    container.configure(configurators)

    running_container = container.start()
    try:
        stream = running_container.stream_logs()
        for line in stream:
            yield line.decode("utf-8")
        result = running_container.inspect()
        exit_code = result["State"]["ExitCode"]
        if exit_code != 0:
            logs = running_container.get_logs()
            console.log(logs)
            raise ContainerException(
                f"container returned with a non-zero exit code {exit_code}", stdout=logs
            )
    finally:
        running_container.shutdown(remove=True)
