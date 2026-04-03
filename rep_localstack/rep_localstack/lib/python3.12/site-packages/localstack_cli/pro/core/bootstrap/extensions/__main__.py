"""Simple internal CLI script used to communicate between the container and the host CLI in
``localstack.pro.core.cli.extensions."""

import json
import os
import traceback

import click
from localstack_cli import config

from . import repository


@click.group("extensions")
def cli():
    from localstack_cli.pro.core.bootstrap import licensingv2
    from localstack_cli.utils.bootstrap import setup_logging

    setup_logging()

    # we activate the license here to make sure that extensions that import encrypted code on
    # module level can be resolved and don't lead to errors
    licensingv2.get_licensed_environment().activate()


@cli.command("debug")
def debug():
    """
    Print debug information about localstack directories and run ``pip list`` in the extensions venv.
    """
    click.echo("Directories")
    click.echo("===========")
    for k, v in config.dirs.__dict__.items():
        click.echo(f"{k:13} {v}")

    click.echo()
    click.echo("Extensions venv")
    click.echo("===============")
    venv = repository.LOCALSTACK_VENV
    click.echo(f"localstack venv: {venv.venv_dir}")
    click.echo(f"  site-packages: {venv.site_dir if venv.exists else 'not initialized'}")
    venv = repository.get_extensions_venv()
    click.echo(f"extensions venv: {venv.venv_dir}")
    click.echo(f"  site-packages: {venv.site_dir if venv.exists else 'not initialized'}")

    click.echo()
    click.echo("pip list")
    click.echo("========")
    os.system(f"bash -c '. {venv.venv_dir}/bin/activate && pip list'")


@cli.command("init")
def init():
    """
    Initialize the extensions repository.
    """
    repository.init_extension_venv()


@cli.command("list")
def list_():
    """Print the extension metadata as NDJSON to stdout."""
    for extension in repository.list_extension_metadata():
        click.echo(json.dumps(extension))


@cli.command("install")
@click.argument("name", required=True)
def install(name: str):
    try:
        repo = repository.ExtensionsRepository()
        for event in repo.run_install(name):
            click.echo(json.dumps(event))
    except Exception as e:
        click.echo(
            json.dumps(
                {
                    "event": "exception",
                    "message": f"Error while installing extension: {e}",
                    "extra": {"traceback": traceback.format_exc()},
                }
            )
        )


@cli.command("uninstall")
@click.argument("name", required=True)
def uninstall(name: str):
    try:
        repo = repository.ExtensionsRepository()
        for event in repo.run_uninstall(name):
            click.echo(json.dumps(event))
    except Exception as e:
        click.echo(
            json.dumps(
                {
                    "event": "exception",
                    "message": f"Error while uninstalling extension: {e}",
                    "extra": {"traceback": traceback.format_exc()},
                }
            )
        )


if __name__ == "__main__":
    cli()
