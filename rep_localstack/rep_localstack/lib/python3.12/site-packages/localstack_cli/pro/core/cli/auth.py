import os

import click
from click import ClickException
from localstack_cli.cli.localstack import LocalStackCliGroup
from localstack_cli.utils.analytics.cli import publish_invocation


@click.group(
    name="auth",
    cls=LocalStackCliGroup,
    short_help="Authenticate with your LocalStack account",
    help="""
    Authenticate with your LocalStack account.

    Manage your credentials and authenticate with your LocalStack account.
    """,
)
def auth() -> None:
    pass


@auth.command(
    name="set-token",
    short_help="Set your Localstack auth token to allow you to start LocalStack Pro",
    help="""
    Configure your auth token. Your auth token is used the license activation to activate LocalStack Pro.
    This is different from `localstack auth login` which enables platform features such as pushing cloud pods to your
    webapp account.

    The auth token you configure here will be passed to the `LOCALSTACK_AUTH_TOKEN` environment variable of the
    LocalStack container when you run `localstack start`.

    AUTH_TOKEN: Your Localstack auth token that you can find in https://app.localstack.cloud.
    """,
)
@click.argument("auth-token", type=str, required=True)
@publish_invocation
def set_token(auth_token: str):
    from localstack_cli.pro.core.bootstrap.licensingv2 import AuthToken

    token = AuthToken(auth_token)
    if not token.is_syntax_valid():
        raise ClickException(
            "The format of the token you provided is invalid, please make sure to set a valid token. Auth "
            "tokens start with `ls-` and are followed by a 36-character string. You can find your auth "
            "token in the LocalStack web app https://app.localstack.cloud."
        )

    if not token.is_checksum_valid():
        raise ClickException(
            "The token you provided appears to be invalid, please make sure to set a valid token. You can "
            "find your auth token in the LocalStack web app https://app.localstack.cloud."
        )

    from localstack_cli.pro.core import config as pro_config
    from localstack_cli.pro.core.bootstrap.auth import get_auth_cache

    try:
        auth_config = get_auth_cache()
        auth_config["LOCALSTACK_AUTH_TOKEN"] = token.encoded()
        auth_config.save()
    except Exception as e:
        raise ClickException(
            f"Could not save auth configuration into {pro_config.AUTH_CACHE_PATH}: {e}"
        ) from e

    click.echo("Token configured successfully")


@auth.command(
    name="clear-token",
    short_help="Clear any existing LocalStack auth token from your environment",
)
@publish_invocation
def clear_token():
    from localstack_cli.pro.core import config as pro_config
    from localstack_cli.pro.core.bootstrap.auth import get_auth_cache
    from localstack_cli.pro.core.bootstrap.licensingv2 import ENV_LOCALSTACK_AUTH_TOKEN, AuthToken

    try:
        auth_config = get_auth_cache()
        if ENV_LOCALSTACK_AUTH_TOKEN not in auth_config:
            click.echo("No token in environment, no change necessary")
            return

        token = AuthToken(auth_config.pop(ENV_LOCALSTACK_AUTH_TOKEN))
        auth_config.save()
        click.echo(f"Token {token} cleared successfully")
    except Exception as e:
        raise ClickException(
            f"Could not save auth configuration into {pro_config.AUTH_CACHE_PATH}: {e}"
        ) from e


@auth.command(
    name="show-token",
    short_help="Show the auth token in your configuration",
    help="""
    Show the token that LocalStack picks up from your environment. This can either be the auth token set via
    `localstack auth set-token`, or the value of `LOCALSTACK_AUTH_TOKEN`.
    """,
)
@click.option(
    "--plain",
    is_flag=True,
    required=False,
    default=False,
    help="""
    Setting this flag will output only the value of the token in plain text, so it can be used as input
    to other programs, like `LOCALSTACK_AUTH_TOKEN=$(localstack auth show-token --plain)`.
    """,
)
@publish_invocation
def show_token(plain: bool):
    from localstack_cli.pro.core import config as pro_config
    from localstack_cli.pro.core.bootstrap.auth import get_auth_cache
    from localstack_cli.pro.core.bootstrap.licensingv2 import ENV_LOCALSTACK_AUTH_TOKEN, AuthToken

    def _print_token_info(_token: AuthToken):
        is_valid = _token.is_valid()
        click.echo(f"Valid: {is_valid}", err=True)
        # will only show parts of the token (ls-xepA3110-****-****-...)
        if plain:
            click.echo(_token.token)
        else:
            click.echo(f"Token: {_token}")

    env_token = os.getenv(ENV_LOCALSTACK_AUTH_TOKEN, "").strip()
    if env_token:
        click.echo(
            f"Prioritizing auth token set in environment variable {ENV_LOCALSTACK_AUTH_TOKEN}",
            err=True,
        )
        _print_token_info(AuthToken(env_token))
        return

    if not os.path.isfile(pro_config.AUTH_CACHE_PATH):
        click.echo(
            "Token not configured in environment yet, please run localstack auth set-token "
            "<AUTH_TOKEN>, or set the environment variable LOCALSTACK_AUTH_TOKEN to a valid auth token. "
            "You can find your auth token in the LocalStack web app https://app.localstack.cloud.",
            err=True,
        )
        return

    try:
        auth_config = get_auth_cache()
    except Exception as e:
        raise ClickException(
            f"Could not load auth configuration from {pro_config.AUTH_CACHE_PATH}: {e}"
        ) from e

    token = auth_config.get(ENV_LOCALSTACK_AUTH_TOKEN, "")
    if not token:
        click.echo(
            "Token not configured in environment yet, please run localstack auth set-token "
            "<AUTH_TOKEN>, or set the environment variable LOCALSTACK_AUTH_TOKEN to a valid auth token. "
            "You can find your auth token in the LocalStack web app https://app.localstack.cloud.",
            err=True,
        )

    _print_token_info(AuthToken(token))


@auth.command(
    name="login",
    short_help="Login to the your LocalStack account",
    help="""
    Login to the LocalStack Platform.

    This command performs a login to your LocalStack account, giving you access to features that require
    platform permissions, such as uploading cloud pods to your account.

    This command is deprecated and it will be removed soon.
    To use LocalStack features that requires authentication to the LocalStack platform
    (e.g., Cloud Pods), please run `localstack auth set-token <AUTH_TOKEN>`, or set the
    environment variable `LOCALSTACK_AUTH_TOKEN` to a valid auth token.
    """,
    deprecated=True,
)
@click.option(
    "-u",
    "--username",
    help="Username (email address) for login",
    metavar="USER",
    required=True,
)
@click.option(
    "-p",
    "--password",
    help="Password for login",
    metavar="PWD",
    prompt=True,
    hide_input=True,
    confirmation_prompt=False,  # don't ask a second time
    required=True,
)
@publish_invocation
def login(username: str, password: str) -> None:
    from localstack_cli.pro.core.bootstrap import auth

    try:
        auth.login(username, password)
        click.echo("successfully logged in")
    except Exception as e:
        raise click.ClickException(f"Authentication Error: {e}")


@auth.command(
    name="logout",
    short_help="Log out from your LocalStack account",
    help="""
    Log out from the LocalStack Platform.

    This command performs a logout from the LocalStack platform and deletes all session information on your
    machine.
    """,
    deprecated=True,
)
@publish_invocation
def logout() -> None:
    from localstack_cli.pro.core.bootstrap import auth

    auth.logout()
    click.echo("successfully logged out")
