"""Core CLI plugin that registers community CLI commands."""
from localstack_cli.cli.plugin import LocalstackCli, LocalstackCliPlugin


class CoreCliPlugin(LocalstackCliPlugin):
    """Plugin that registers core CLI commands."""

    name = "core"

    def attach(self, cli: LocalstackCli) -> None:
        # Core commands are already on the main group, nothing to add
        pass
