import json

import click
import requests
from localstack_cli import constants as localstack_constants
from localstack_cli.cli import console
from localstack_cli.cli.exceptions import CLIError
from localstack_cli.pro.core.bootstrap import auth
from localstack_cli.pro.core.cli.cli import RequiresLicenseGroup
from localstack_cli.utils.analytics.cli import publish_invocation

API_ENDPOINT = localstack_constants.API_ENDPOINT
API_CREATION_ENDPOINT = f"{API_ENDPOINT}/compute/instances"
API_DELETION_ENDPOINT = f"{API_ENDPOINT}/compute/instances/{{name}}"
API_LIST_ENDPOINT = f"{API_ENDPOINT}/compute/instances"
API_LOGS_ENDPOINT = f"{API_ENDPOINT}/compute/instances/{{name}}/logs"


@click.group(
    name="ephemeral",
    short_help="(Preview) Manage ephemeral LocalStack instances",
    help="""
    (Preview) Manage ephemeral LocalStack instances in the cloud.

    This command group allows you to create, list, and delete ephemeral LocalStack instances.
    Ephemeral instances are temporary cloud instances that can be used for testing and development.
    """,
    cls=RequiresLicenseGroup,
)
def ephemeral() -> None:
    pass


@ephemeral.command(
    name="create",
    short_help="Create a new ephemeral instance",
    help="""
    Create a new ephemeral LocalStack instance in the cloud.

    Specify an instance name and optional parameters like lifetime and environment variables.
    The instance will be created with the specified configuration and its connection details will be returned.

    \b
    Examples:
        localstack ephemeral create --name my-test-instance
        localstack ephemeral create --name my-instance --lifetime 60
        localstack ephemeral create --name my-instance --env DEBUG=1
    """,
)
@click.option("--name", required=True, help="Name of the ephemeral instance")
@click.option("--lifetime", required=False, type=int, help="Lifetime of the instance in minutes")
@click.option(
    "--env",
    "-e",
    help="Additional environment variables that are passed to the LocalStack instance",
    multiple=True,
    required=False,
)
@publish_invocation
def create(
    name: str,
    lifetime: int | None,
    env: tuple | None,
) -> None:
    """Create a new ephemeral instance with the specified configuration."""
    try:
        env_dict = {}
        if env:
            for var in env:
                if "=" not in var:
                    raise CLIError(f"Invalid environment variable format: {var}")
                key, value = var.split("=", 1)
                env_dict[key.strip()] = value.strip()

        headers = auth.get_platform_auth_headers()
        data = {
            "instance_name": name,
            "lifetime": lifetime or 60,
            "env_vars": env_dict,
        }

        response = requests.post(API_CREATION_ENDPOINT, headers=headers, json=data)
        response.raise_for_status()

        console.print_json(json.dumps(response.json()))

    except requests.exceptions.RequestException as e:
        if hasattr(e, "response") and e.response is not None:
            try:
                error_detail = e.response.json()
                raise CLIError(f"Failed to create ephemeral instance: {error_detail}")
            except json.JSONDecodeError:
                raise CLIError(f"Failed to create ephemeral instance: {str(e)}")
        raise CLIError(f"Failed to create ephemeral instance: {str(e)}")


@ephemeral.command(
    name="list",
    short_help="List all ephemeral instances",
    help="""
    List all available ephemeral LocalStack instances.

    This command shows all ephemeral instances associated with your account,
    including their names, status, and other relevant details.

    \b
    Examples:
        localstack ephemeral list
    """,
)
@publish_invocation
def list_instances() -> None:
    """List all ephemeral instances."""
    try:
        headers = auth.get_platform_auth_headers()

        response = requests.get(API_LIST_ENDPOINT, headers=headers)
        response.raise_for_status()

        response_data = response.json()
        console.print_json(json.dumps(response_data, indent=2))

    except requests.exceptions.RequestException as e:
        raise CLIError(f"Failed to list ephemeral instances: {str(e)}")


@ephemeral.command(
    name="delete",
    short_help="Delete an ephemeral instance",
    help="""
    Delete a specific ephemeral LocalStack instance.

    Specify the name of the instance you want to delete.
    Once deleted, the instance cannot be recovered.

    \b
    Example:
        localstack ephemeral delete --name my-test-instance
    """,
)
@click.option("--name", required=True, help="Name of the ephemeral instance to delete")
@publish_invocation
def delete(name: str) -> None:
    """Delete an ephemeral instance."""
    try:
        url = API_DELETION_ENDPOINT.format(name=name)
        headers = auth.get_platform_auth_headers()

        response = requests.delete(url, headers=headers)
        response.raise_for_status()

        console.print(f"Successfully deleted instance: {name} ✅")

    except requests.exceptions.RequestException as e:
        raise CLIError(f"Failed to delete ephemeral instance: {str(e)}")


@ephemeral.command(
    name="logs",
    short_help="Fetch logs from an ephemeral instance",
    help="""
    Fetch logs from a specific ephemeral LocalStack instance.

    Retrieve the logs of a running ephemeral instance by specifying its name.
    The logs are returned in chronological order.

    \b
    Example:
        localstack ephemeral logs --name my-test-instance
    """,
)
@click.option("--name", required=True, help="Name of the ephemeral instance to fetch logs from")
@publish_invocation
def logs(name: str) -> None:
    """Fetch logs from an ephemeral instance."""
    try:
        url = API_LOGS_ENDPOINT.format(name=name)
        headers = auth.get_platform_auth_headers()

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        log_lines = response.json()
        if not log_lines:
            console.print("No logs available for this instance.")
            return

        for log_line in log_lines:
            content = log_line.get("content", "")
            console.print(f"{content}")

    except requests.exceptions.RequestException as e:
        if hasattr(e, "response") and e.response is not None:
            try:
                error_detail = e.response.json()
                raise CLIError(f"Failed to fetch logs: {error_detail}")
            except json.JSONDecodeError:
                raise CLIError(f"Failed to fetch logs: {str(e)}")
        raise CLIError(f"Failed to fetch logs: {str(e)}")
