import functools

from plux import PluginManager, plugin

# plugin namespace constants
HOOKS_CONFIGURE_LOCALSTACK_CONTAINER = "localstack_cli.hooks.configure_localstack_container"
HOOKS_PREPARE_HOST = "localstack_cli.hooks.prepare_host"


def hook(namespace: str, priority: int = 0, **kwargs):
    """
    Decorator for creating functional plugins that have a hook_priority attribute. Hooks with a higher priority value
    will be executed earlier.
    """

    def wrapper(fn):
        fn.hook_priority = priority
        return plugin(namespace=namespace, **kwargs)(fn)

    return wrapper


def hook_spec(namespace: str):
    """
    Creates a new hook decorator bound to a namespace.

    on_infra_start = hook_spec("localstack.hooks.on_infra_start")

    @on_infra_start()
    def foo():
        pass

    # run all hooks in order
    on_infra_start.run()
    """
    fn = functools.partial(hook, namespace=namespace)
    # attach hook manager and run method to decorator for convenience calls
    fn.manager = HookManager(namespace)
    fn.run = fn.manager.run_in_order
    return fn


class HookManager(PluginManager):
    def load_all_sorted(self, propagate_exceptions=False):
        """
        Loads all hook plugins and sorts them by their hook_priority attribute.
        """
        plugins = self.load_all(propagate_exceptions)
        # the hook_priority attribute is part of the function wrapped in the FunctionPlugin
        plugins.sort(
            key=lambda _fn_plugin: getattr(_fn_plugin.fn, "hook_priority", 0), reverse=True
        )
        return plugins

    def run_in_order(self, *args, **kwargs):
        """
        Loads and runs all plugins in order them with the given arguments.
        """
        for fn_plugin in self.load_all_sorted():
            fn_plugin(*args, **kwargs)

    def __str__(self):
        return f"HookManager({self.namespace})"

    def __repr__(self):
        return self.__str__()


configure_localstack_container = hook_spec(HOOKS_CONFIGURE_LOCALSTACK_CONTAINER)
"""Hooks to configure the LocalStack container before it starts. Executed on the host when invoking the CLI."""

prepare_host = hook_spec(HOOKS_PREPARE_HOST)
"""Hooks to prepare the host that's starting LocalStack. Executed on the host when invoking the CLI."""
