"""Runtime utilities for the LocalStack CLI."""

from localstack_cli.runtime import hooks  # noqa: F401

# get_current_runtime is not available in the standalone CLI - only in the full LocalStack runtime
get_current_runtime = None
