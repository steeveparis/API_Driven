import click
from localstack_cli.pro.core.cli.cli import RequiresLicenseGroup


@click.group(
    name="aws",
    short_help="Access additional functionality on LocalStack AWS Services",
    help="""
    Accesses additional functionality on LocalStack emulated AWS services.

    This command provides tools to enhance your experience with certain emulated AWS services.
    """,
    cls=RequiresLicenseGroup,
)
def aws() -> None:
    pass
