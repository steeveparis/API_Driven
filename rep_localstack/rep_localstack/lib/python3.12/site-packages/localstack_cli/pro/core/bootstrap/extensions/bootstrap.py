"""Hooks for extension developer mode on the host CLI."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from tempfile import gettempdir

from localstack_cli import config
from localstack_cli.utils.bootstrap import Container
from localstack_cli.utils.container_utils.container_client import BindMount
from localstack_cli.utils.json import FileMappedDocument
from localstack_cli.utils.strings import md5

LOG = logging.getLogger(__name__)

_ENTRYPOINT_SCRIPT = """#!/bin/bash
echo "=================================================="
echo "LocalStack extension developer mode enabled"
shopt -s nullglob

pkgs=$(echo /opt/code/localstack/.venv/lib/python3*/site-packages)

echo "import localstack_extensions_resolve;localstack_extensions_resolve.resolve()" > ${pkgs}/localstack-extensions-resolve.pth

cat << EOF >> ${pkgs}/localstack_extensions_resolve.py
import os
import sys
import glob

base_dirs_visited = set()

def append_pth_recursively(base_dir):
    if base_dir in base_dirs_visited:
        return
    base_dirs_visited.add(base_dir)

    for f in glob.glob(f"{base_dir}/*.pth", recursive=True):
        with open(f, "r") as fd:
            abs_path = os.path.abspath(os.path.dirname(f))
            lines = fd.readlines()
            for line in lines:
                if line := line.strip():
                    if "import" in line:
                        continue
                    module_dir = os.path.abspath(os.path.join(abs_path, line)) if not os.path.isabs(line) else line
                    if os.path.exists(module_dir) and os.path.isdir(module_dir):
                        if module_dir not in sys.path:
                            sys.path.append(module_dir)
                        append_pth_recursively(module_dir)


def resolve():
    append_pth_recursively(os.path.dirname(__file__))
EOF

for d in /opt/code/extensions/* ;
do
    echo "- mounting extension ${d}"
    find ${d} -type d -name "site-packages" >> ${pkgs}/localstack-extensions-venv.pth
    echo ${d} >> ${pkgs}/localstack-extensions-venv.pth
done
echo "Resuming normal execution, ..."
echo "=================================================="
exec /usr/local/bin/docker-entrypoint.sh
"""

_host_extension_dirs: list[str] = []


def run_on_configure_host_hook():
    """Load extension directories from ~/.localstack/extensions-dev.json."""
    doc = FileMappedDocument(os.path.join(config.CONFIG_DIR, "extensions-dev.json"))

    for extensions_spec in doc.get("extensions", []):
        path = extensions_spec.get("host_path")
        if path and os.path.exists(path):
            _host_extension_dirs.append(path)


def run_on_configure_localstack_container_hook(container: Container):
    """Configure container for extension dev mode."""
    # Create and mount custom entrypoint script
    h = md5(_ENTRYPOINT_SCRIPT)
    file = Path(gettempdir(), f"docker-entrypoint-{h}.sh")
    if not file.exists():
        file.write_text(_ENTRYPOINT_SCRIPT, newline="\n", encoding="utf-8")
        file.chmod(0o777)

    container.config.volumes.add(BindMount(str(file), f"/tmp/{file.name}"))
    container.config.entrypoint = f"/tmp/{file.name}"

    # Mount extension directories
    for ext_dir in _host_extension_dirs:
        target = os.path.join("/opt/code/extensions", os.path.basename(ext_dir))
        container.config.volumes.add(BindMount(ext_dir, target, read_only=True))
