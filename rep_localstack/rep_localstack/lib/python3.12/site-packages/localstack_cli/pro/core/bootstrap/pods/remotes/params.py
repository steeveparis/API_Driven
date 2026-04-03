import logging
import os
from collections.abc import Callable
from urllib.parse import urlparse

from localstack_cli.pro.core.bootstrap.pods.remotes.configs import DEFAULT_REMOTE_SCHEME

LOG = logging.getLogger(__name__)

PARAM_ACCESS_KEY_ID = "access_key_id"
PARAM_SECRET_ACCESS_KEY = "secret_access_key"
PARAM_SESSION_TOKEN = "session_token"


def _get_aws_credentials_from_boto_session() -> dict[str, str] | None:
    try:
        import boto3

        session = boto3.session.Session()
        credentials = session.get_credentials()
        return {
            PARAM_ACCESS_KEY_ID: credentials.access_key,
            PARAM_SECRET_ACCESS_KEY: credentials.secret_key,
            PARAM_SESSION_TOKEN: credentials.token,
        }
    except Exception as e:
        LOG.debug("Unable to extract remote parameters: %s", e)


def get_s3_remote_params() -> dict[str, str]:
    """
    Returns the AWS credentials necessary to create the bucket storing the pods artifacts.
    It first tries to fetch the credentials from a boto3 session. If this is not possible (e.g., boto3 not installed
    on the host), it looks in the environment.
    """
    if params := _get_aws_credentials_from_boto_session():
        return params

    aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_session_token = os.getenv("AWS_SESSION_TOKEN")

    if not aws_access_key_id or not aws_secret_access_key:
        raise Exception(
            "Please export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the environment"
        )

    params = {
        PARAM_ACCESS_KEY_ID: aws_access_key_id,
        PARAM_SECRET_ACCESS_KEY: aws_secret_access_key,
    }
    if aws_session_token:
        params[PARAM_SESSION_TOKEN] = aws_session_token
    return params


def get_oras_remote_params() -> dict[str, str]:
    # note: allow case-insensitive env. variables (all upper- or lower-case)
    oras_username = os.getenv("ORAS_USERNAME") or os.getenv("oras_username")
    oras_password = os.getenv("ORAS_PASSWORD") or os.getenv("oras_password")
    if not oras_username or not oras_password:
        raise Exception("Please specify ORAS_USERNAME and ORAS_PASSWORD in the environment")
    return {"oras_username": oras_username, "oras_password": oras_password}


def get_platform_remote_params() -> dict[str, str]:
    # note: allow case-insensitive env. variables (all upper- or lower-case)
    auth_token = os.getenv("LOCALSTACK_AUTH_TOKEN")
    bearer_token = os.getenv("LOCALSTACK_BEARER_TOKEN")
    api_key = os.getenv("LOCALSTACK_API_KEY")
    if not auth_token and not api_key and not bearer_token:
        raise Exception("Please specify LOCALSTACK_AUTH_TOKEN in the environment")
    return {"api_key": api_key, "auth_token": auth_token, "bearer_token": bearer_token}


remotes_protocols: dict[str, Callable[[], dict]] = {
    "s3": get_s3_remote_params,
    "oras": get_oras_remote_params,
    "platform": get_platform_remote_params,
}


def get_remote_params_callable(url: str) -> Callable[[], dict] | None:
    """Returns a Callable that retrieves the remote parameters for a given URL."""
    protocol = urlparse(url).scheme or DEFAULT_REMOTE_SCHEME
    return remotes_protocols.get(protocol, None)
