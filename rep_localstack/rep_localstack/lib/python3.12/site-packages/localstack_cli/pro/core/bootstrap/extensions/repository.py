import inspect
import logging
import os
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any, Literal, TypedDict

from localstack_cli import config, constants
from localstack_cli.utils.objects import singleton_factory
from localstack_cli.utils.venv import VirtualEnvironment
from plux import PluginManager

LOG = logging.getLogger(__name__)

LOCALSTACK_VENV = VirtualEnvironment(constants.LOCALSTACK_VENV_FOLDER)

VENV_DIRECTORY = "extensions/python_venv"


def get_extensions_venv() -> VirtualEnvironment:
    """
    Returns a VirtualEnvironment object point to ``<var_libs>/extensions/python_venv``, either on the host
    or in the container.

    :return: the virtual environment
    """
    return VirtualEnvironment(os.path.join(config.dirs.var_libs, VENV_DIRECTORY))


@singleton_factory
def get_extension_repository() -> "ExtensionsRepository":
    """
    Returns the global ExtensionRepository and ensures that the localstack extension venv was created
    correctly.

    :return: an ExtensionsRepository instance
    """
    return ExtensionsRepository(init_extension_venv())


def init_extension_venv() -> VirtualEnvironment:
    """
    Idempotent operation to ensure the extensions virtual environment is created, and the localstack venv
    is linked into it via a .pth file.
    """
    venv = get_extensions_venv()

    if not venv.exists:
        LOG.info("initializing extension environment at %s", venv.venv_dir)
        venv.create()
        LOG.debug("adding localstack venv path %s to %s", LOCALSTACK_VENV, venv.venv_dir)
        venv.add_pth("localstack-venv", LOCALSTACK_VENV)

    return venv


def list_extension_metadata() -> list[dict]:
    """
    Lists all available extensions and their distribution metadata.

    :return: list of metadata documents
    """
    from localstack_cli.extensions.api import Extension

    return list_plugin_distribution_data(PluginManager(Extension.namespace))


def list_plugin_distribution_data(plugin_manager: PluginManager) -> list[dict]:
    """
    Returns a list of dictionaries containing plugin metadata.

    :param plugin_manager: the plugin manager holding the plugins
    :return: a list of metadata documents
    """
    metadata = []

    for plugin in plugin_manager.list_containers():
        try:
            dist = plugin.distribution
        except ValueError:
            continue
        except Exception as e:
            LOG.error(
                "Error while resolving distribution for plugin %s: %s. This probably means that the "
                "package was removed or otherwise changed after the plugin was loaded. Restarting LocalStack "
                "should fix the issue.",
                plugin.name,
                e,
            )
            continue
        if not dist:
            continue

        spec = plugin.plugin_spec
        module = inspect.getmodule(plugin.plugin_spec.factory)

        doc = {
            "namespace": spec.namespace,
            "name": plugin.name,
            "factory": {
                "module": str(module.__name__),
                "code": f"{module.__name__}.{spec.factory.__name__}",
                "file": str(module.__file__),
            },
            "distribution": dist.metadata.json,
        }
        metadata.append(doc)

    return metadata


class InstallerEvent(TypedDict, total=False):
    event: Literal["status", "log", "error", "exception", "pip", "extension"]
    message: str
    extra: dict[str, Any] | None


class ExtensionsRepository:
    """
    Helper class around a VirtualEnvironment that can install and uninstall packages into the venv.
    """

    venv: VirtualEnvironment

    def __init__(self, venv: VirtualEnvironment = None):
        self.venv = venv or get_extensions_venv()
        self.venv.inject_to_sys_path()

    @property
    def pip(self) -> Path:
        """
        Returns the path to the pip binary within the virtual environment, or raises a FileNotFoundError if it
        does not exist.

        :raises FileNotFoundError: if the pip binary wasn't found
        :return: a path
        """
        # TODO: move to VirtualEnvironment
        pip = self.venv.venv_dir / "bin" / "pip"
        if not pip.exists():
            raise FileNotFoundError(f"pip is not available at {self.pip}")
        return pip

    def pip_show(self, package: str) -> dict | None:
        """
        Runs `pip show <package>` in the virtual environment and returns the output as a dictionary if the
        package exist, or None if it doesn't exist.

        :param package: the package to look up
        :return: the metadata of the package or None
        """
        # TODO: move to VirtualEnvironment
        cmd = [self.pip, "show", package]
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
            return dict(line.split(": ", maxsplit=1) for line in output.splitlines())
        except subprocess.CalledProcessError as e:
            if "not found" in e.output:
                return None
            raise

    def run_install(self, name_or_url: str) -> Generator[InstallerEvent, None, None]:
        """
        Creates a new installer generator to install the given package or URL. The ``name_or_url``
        parameter is used directly as input for ``pip install``. The generator yields ``InstallerEvents``
        as the installation procedure progresses.

        :param name_or_url: the pypi package name or URL to install
        :return: a generator
        """
        cmd = [
            self.pip,
            "--no-input",
            "--no-color",
            "--disable-pip-version-check",
            "install",
            name_or_url,
        ]

        yield {"event": "status", "message": "Checking installed extensions"}

        metadata = self.pip_show(name_or_url)
        if metadata:
            name = metadata["Name"]
            summary = metadata["Summary"]
            author = metadata["Author"]
            yield {
                "event": "log",
                "message": f"Extension {name} ({summary} by {author}) already installed",
            }
            return

        # load extensions that are installed now
        _clear_plugin_cache()
        before = {item["name"]: item for item in list_extension_metadata()}

        yield {"event": "status", "message": "Installing extension"}

        not_found = False
        try:
            with SubprocessLineStream.open(cmd) as stream:
                for line in stream:
                    yield {"event": "pip", "message": line}
                    if "No matching distribution found for" in line:
                        not_found = True
                        yield {
                            "event": "error",
                            "message": f"Could not resolve package {name_or_url}, please check the URL or "
                            "that the package exists in pypi.",
                        }
        except subprocess.CalledProcessError:
            if not_found:
                return
            raise

        # re-load all extension metadata and compare to the ones that were installed
        _clear_plugin_cache()
        after = {item["name"]: item for item in list_extension_metadata()}
        installed = [v for k, v in after.items() if k not in before]

        if installed:
            yield {"event": "log", "message": "Extension successfully installed"}

            for extension in installed:
                yield {"event": "extension", "message": "", "extra": extension}
        else:
            yield {"event": "log", "message": "No change"}

        yield {"event": "status", "message": "Extension installation completed"}

    def run_uninstall(self, package: str) -> Generator[InstallerEvent, None, None]:
        """
        Like ``run_install``, only it performs a ``pip uninstall`` operation.

        :param package: the package name
        :return: a InstallerEvent generator
        """
        cmd = [
            self.pip,
            "--no-input",
            "--no-color",
            "--disable-pip-version-check",
            "uninstall",
            "-y",
            package,
        ]

        yield {"event": "status", "message": "Checking extensions"}

        metadata = self.pip_show(package)
        if not metadata:
            yield {"event": "log", "message": f"Extension {package} is not installed"}
            return

        name = metadata["Name"]
        summary = metadata["Summary"]
        yield {
            "event": "log",
            "message": f"Uninstalling extension {name} ({summary})",
        }
        yield {"event": "status", "message": "Uninstalling extension"}

        with SubprocessLineStream.open(cmd) as stream:
            for line in stream:
                yield {"event": "pip", "message": line}

        yield {"event": "log", "message": "Extension successfully uninstalled"}
        _clear_plugin_cache()
        yield {"event": "status", "message": "Extension uninstall completed"}


class SubprocessLineStream:
    """
    Class to help with the pattern of streaming the output of a subprocess line-by-line. A
    SubprocessLineStream can be closed which will terminate the underlying process. The stream will
    automatically wait for and terminate the command after EOF is reached. It also automatically detects
    text mode and rstrips newlines.

    TODO: move into run utils in LocalStack

    Best enjoyed as follows::

        with SubprocessLineStream.open(["cowsay", "hello"]) as stream:
            for line in stream:
                print("|", line)

            print(stream.process.returncode)

    Will print::

        |  _______
        | < hello >
        |  -------
        |         \\   ^__^
        |          \\  (oo)\\_______
        |             (__)\\       )\\/\
        |                 ||----w |
        |                 ||     ||
        0
    """

    default_timeout: int = 5
    """Time in seconds the stream will wait for the process output to finish before moving on."""

    def __init__(self, process: subprocess.Popen):
        self.process = process

    def __iter__(self):
        return self._gen()

    def _gen(self):
        stream = self.process.stdout

        if self.process.text_mode:
            newlines = "\r\n"
        else:
            newlines = b"\r\n"

        for line in stream:
            yield line.rstrip(newlines)

        if self.process.wait(self.default_timeout) != 0:
            raise subprocess.CalledProcessError(
                returncode=self.process.returncode,
                cmd=self.process.args,
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self.process.terminate()

    @classmethod
    def open(cls, cmd, *args, **kwargs):
        """
        Creates a new subprocess.Popen object with the given args with some sane defaults (mapping stderr to
        stdout, and using text=True), wrapped in this class.

        :param cmd: the command to run
        :param args: args passed to subprocess.Popen
        :param kwargs: keyword args passed to subprocess.Popen
        :return:
        """
        return cls(
            subprocess.Popen(
                cmd,
                *args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                **kwargs,
            )
        )


def _clear_plugin_cache():
    """Clears the underlying caches used by plux to resolve entry points and distribution metadata."""
    # FIXME: this is necessary to re-load the entrypoint cache when trying to resolve extension
    #  before/after install, but bad because it leaks internals.
    from plux.runtime.cache import EntryPointsCache
    from plux.runtime.metadata import packages_distributions

    # plux cache that stores resolved entry points. this will force a re-calculation of the path hashing.
    cache = EntryPointsCache.instance()
    with cache._lock:
        cache._cache.clear()

    # plux cache that stores distribution data from importlib.metadata
    packages_distributions.cache_clear()
