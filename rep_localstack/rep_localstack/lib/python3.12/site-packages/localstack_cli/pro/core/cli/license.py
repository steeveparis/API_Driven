import os.path

import click
from localstack_cli.pro.core.bootstrap.licensingv2 import (
    AuthToken,
    DevLocalstackEnvironment,
    LicensingError,
    get_credentials_from_environment,
    get_licensed_environment,
)


@click.group(
    name="license",
    short_help="(Preview) Manage and verify your LocalStack license",
    help="""
    (Preview) Manage and verify your LocalStack license.

    Your LocalStack license allows you to use advanced features of LocalStack.
    """,
)
def license() -> None:
    pass


@license.command("info")
def cmd_info():
    try:
        credentials = get_credentials_from_environment()
    except Exception as e:
        click.echo(f"credentials: {e}")
        return

    if credentials:
        if isinstance(credentials, AuthToken):
            if credentials.encoded() == "test":
                credential_validity = "test"
            else:
                credential_validity = "valid syntax" if credentials.is_valid() else "invalid"
        else:
            credential_validity = ""

        click.echo(
            f"credentials: {type(credentials).__name__}({credentials.encoded()}) {credential_validity}"
        )
    else:
        click.echo("credentials: none found in environment")

    env = get_licensed_environment()
    if isinstance(env, DevLocalstackEnvironment):
        click.echo("test credentials used, using unlicensed dev environment")
        return

    license_file_path = None
    for path in env.get_license_file_read_locations():
        if os.path.isfile(path):
            license_file_path = path
            break

    license_ = None
    if license_file_path:
        try:
            with open(license_file_path, "rb") as fd:
                content = fd.read()
                license_ = env.parser.parse(content)
                text_content = content.decode("utf-8")
                click.echo(f"license location: {license_file_path}")
                click.echo(f"license: {text_content}")
        except LicensingError as e:
            click.echo(f"license location: {license_file_path}")
            click.echo(f"license: error reading license file {e}")

    if license_:
        try:
            env.client.validate_license(env.require_valid_credentials(), license_)
            click.echo("license validity: valid")
        except LicensingError as e:
            click.echo(f"license validity: invalid ({e})")


@license.command("activate")
def cmd_activate():
    try:
        env = get_licensed_environment()
        env.activate()
        text_license = env.serializer.serialize(env.license).decode("utf-8")
        click.echo("license activation completed")
        click.echo(f"license: {text_license}")
    except LicensingError as e:
        click.echo(e.get_user_friendly())
