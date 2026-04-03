"""
Used from the docker-entrypoint.sh to auto-install extensions configured in ``EXTENSION_AUTO_INSTALL`` or
located in ``/etc/localstack/conf.d/extensions.txt``.
"""

import os.path
import traceback

from localstack_cli import config
from localstack_cli.pro.core import config as pro_config

from .repository import SubprocessLineStream, get_extension_repository


def log(msg: str):
    print(f"Localstack extensions installer: {msg}")


def main():
    if not pro_config.ACTIVATE_PRO:
        return

    extensions_txt = os.path.join(config.dirs.config, "extensions.txt")
    auto_installable = pro_config.EXTENSION_AUTO_INSTALL

    if not os.path.exists(extensions_txt) and not auto_installable:
        # no extensions to install
        return

    # this will create an extensions venv directory if it doesn't exist
    repo = get_extension_repository()

    # install extensions.txt first
    if os.path.exists(extensions_txt):
        log(f"installing extensions defined in {extensions_txt}")
        cmd = [
            repo.pip,
            "install",
            "--no-input",
            "--no-color",
            "--disable-pip-version-check",
            "-r",
            extensions_txt,
        ]
        with SubprocessLineStream.open(cmd) as stream:
            for line in stream:
                log(line)

    # install from variable next
    log("installing extensions defined EXTENSIONS_AUTO_INSTALL")
    for name_or_url in auto_installable:
        log(f"auto installing extension {name_or_url}")
        # TODO: proper output formatting
        try:
            for event in repo.run_install(name_or_url=name_or_url):
                log(event.get("message"))
        except Exception as e:
            log(f"{e}")
            log(traceback.format_exc())


if __name__ == "__main__":
    main()
