import logging
from typing import Any

import click
from localstack_cli.cli import LocalstackCli, LocalstackCliPlugin, console
from localstack_cli.utils.analytics.cli import publish_invocation

from .auth import auth
from .aws import aws
from .cli import RequiresLicenseGroup
from .cloud_pods import pod
from .ephemeral import ephemeral
from .extensions import extensions
from .iam import iam  # noqa
from .license import license
from .replicator import replicator
from .state import state


class ProCliPlugins(LocalstackCliPlugin):
    """Plugin that adds more CLI commands to the localstack CLI. Some commands are subject to a license
    activation, but all of them are shown by default."""

    name = "pro"

    def attach(self, cli: LocalstackCli) -> None:
        group: click.Group = cli.group
        group.add_command(dns)
        group.add_command(aws)  # noqa
        group.add_command(extensions)  # noqa
        group.add_command(license)  # noqa
        group.add_command(state)  # noqa
        group.add_command(auth)  # noqa
        group.add_command(pod)  # noqa
        group.add_command(ephemeral)  # noqa
        group.add_command(replicator)  # noqa


@click.group(
    name="dns",
    short_help="Manage LocalStack DNS host config",
    help="""
    Manage the usage of the LocalStack DNS on your host.

    This command provides tools to configure your the DNS on your host machine to use the LocalStack DNS
    on your host machine.
    The LocalStack DNS is used for certain Pro features (like the transparent endpoint injection).

    \b
    Visit https://docs.localstack.cloud/user-guide/tools/transparent-endpoint-injection/dns-server/
    for more information on the LocalStack DNS and how it is used.
    """,
    cls=RequiresLicenseGroup,
)
def dns() -> None:
    pass


@dns.command(
    name="systemd-resolved",
    short_help="Manage LocalStack DNS in systemd-resolved",
    help="""
        Manage the LocalStack DNS configuration using systemd-resolved (Ubuntu, Debian, etc.).

        This command sets (or reverts) the LocalStack DNS, running in the current LocalStack runtime, in
        systemd-resolved for the docker network interface.
        Most current Linux systems - like Ubuntu, Debian, or Fedora - use systemd-resolved for the network name
        resolution.
    """,
)
@click.option("--set/--revert", "-s/-r", "set_", default=True, help="Set or revert DNS settings")
@publish_invocation
def cmd_dns_systemd(set_: bool) -> None:
    import localstack_cli.pro.core.bootstrap.dns_utils
    from localstack_cli.pro.core.bootstrap.dns_utils import configure_systemd

    console.print("Configuring systemd-resolved...")
    logger_name = localstack_cli.pro.core.bootstrap.dns_utils.LOG.name
    localstack_cli.pro.core.bootstrap.dns_utils.LOG = ConsoleLogger(logger_name)
    configure_systemd(not set_)


class ConsoleLogger(logging.Logger):
    def __init__(self, name):
        super().__init__(name)

    def info(
        self,
        msg: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        console.print(msg % args)

    def warning(
        self,
        msg: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        console.print("[red]Warning:[/red] ", msg % args)

    def error(
        self,
        msg: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        console.print("[red]Error:[/red] ", msg % args)

    def exception(
        self,
        msg: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        console.print("[red]Error:[/red] ", msg % args)
        console.print_exception()
