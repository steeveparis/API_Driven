import base64
import getpass
import json
import logging
import sys
import time
from typing import Any

import jwt
from localstack_cli.constants import API_ENDPOINT
from localstack_cli.pro.core import config as pro_config
from localstack_cli.pro.core.bootstrap.licensingv2 import (
    ApiKeyCredentials,
    get_credentials_from_environment,
)
from localstack_cli.pro.core.bootstrap.licensingv2 import (
    AuthToken as LSAuthToken,
)
from localstack_cli.pro.core.constants import VERSION
from localstack_cli.utils.functions import call_safe
from localstack_cli.utils.http import safe_requests
from localstack_cli.utils.json import FileMappedDocument
from localstack_cli.utils.strings import to_bytes, to_str

LOG = logging.getLogger(__name__)


AuthCache = FileMappedDocument | dict

# TODO: reconcile this module with localstack.bootstrap.licensingv2


class AuthToken:
    """A thin wrapper around auth tokens, as not all of them a just strings but can also
    include some other important details, like refresh token, expiration time etc."""

    def __init__(self, token: str, metadata: dict | None = None):
        self.token = token
        self.metadata = metadata

    def extract_jwt(self):
        """Extract the JWT token from the given auth token (e.g., "Bearer <jwt_token>")."""
        jwt_token = self.token.strip().split(" ")[-1]
        # attempt to parse the token, then return it
        jwt.decode(jwt_token, options={"verify_signature": False})
        return jwt_token

    def to_dict(self) -> dict[str, Any]:
        return {**(self.metadata or {}), "token": self.token}


class AuthClient:
    """Authentication client used to obtain auth tokens from the remote API endpoint"""

    # token refresh lead time (secs before token expiry to initiate token refresh)
    TOKEN_REFRESH_LEAD_TIME = 30

    def get_auth_token(
        self, username: str, password: str, headers: dict | None = None
    ) -> AuthToken | None:
        data = {"username": username, "password": password}
        response = safe_requests.post(
            self._api_url("/user/signin"), json.dumps(data), headers=headers
        )
        if not response.ok:
            return None
        try:
            result = json.loads(to_str(response.content or "{}"))
            return AuthToken(token=result["token"], metadata=result)
        except Exception:
            pass

    def get_token_expiry(self, token: AuthToken) -> int | None:
        """Get the expiry (epoch timestamp) of the given auth token, or None if this is not a valid (JWT) token"""
        try:
            # note: not performing token verification here, extracting expiry date directly
            claims = jwt.decode(token.extract_jwt(), options={"verify_signature": False})
            return claims.get("exp")
        except jwt.PyJWTError:
            # looks like this is not a JWT token (e.g., when using legacy auth tokens) -> return
            return

    def refresh_token(self, token: AuthToken) -> AuthToken:
        """Check the expiry of the given token, and perform token refresh if it is (about to) expire(d)"""
        expiry = self.get_token_expiry(token)
        if not expiry or time.time() < expiry - self.TOKEN_REFRESH_LEAD_TIME:
            return token

        data = token.to_dict()
        response = safe_requests.put(self._api_url("/user/signin"), json.dumps(data))
        if not response.ok:
            raise Exception(
                f"Unable to obtain auth token (code {response.status_code}) - please log in again."
            )
        try:
            result = json.loads(to_str(response.content or "{}"))
            token_with_metadata = result["token"]
            return AuthToken(token=token_with_metadata["token"], metadata=token_with_metadata)
        except Exception as e:
            raise Exception(f"Unable to obtain token ({e}) - please log in again.")

    def read_credentials(
        self, username: str | None = None, password: str | None = None
    ) -> tuple[str, str, dict]:
        # TODO: try to read from a configured credentials file in config.dirs.config
        # if username and password is not specified, prompt for input
        if not username or not password:
            sys.stdout.write("Please provide your login credentials below\n")
            sys.stdout.flush()
        if not username:
            sys.stdout.write("Username: ")
            sys.stdout.flush()
            username = input()
        if not password:
            password = getpass.getpass()
        return username, password, {}

    def _api_url(self, path: str) -> str:
        return f"{API_ENDPOINT}{path}"


def get_auth_cache() -> FileMappedDocument:
    """Auth cache for the host on the filesystem."""
    return FileMappedDocument(pro_config.AUTH_CACHE_PATH, mode=0o600)


def login(username: str | None = None, password: str | None = None) -> None:
    """
    Authenticate against the /user/signin endpoint. If credentials are correct, it stores the token in the cache.
    The bearer token can be used to sub-sequentially authenticate various calls to platform, e.g., Cloud Pod operations.
    :param username: the username for the login.
    :param password: the password for the login.
    :raise an exception if the credentials are not valid.
    """
    auth_client = AuthClient()
    username, password, headers = auth_client.read_credentials(username, password)
    print("Verifying credentials ... (this may take a few moments)")
    token = auth_client.get_auth_token(username, password, headers)
    if not token:
        raise Exception("Unable to verify login credentials - please try again")

    # cache generated token
    cache = get_auth_cache()
    cache.update({"username": username, "token": token.token})
    # a permission error can occur if the cache directory was created previously by a container
    call_safe(cache.save, exception_message="error saving authentication information")


def logout() -> None:
    """A logout operation clears the username and the token from the cache."""
    cache = get_auth_cache()
    cache.pop("username", None)
    cache.pop("token", None)
    cache.save()


def get_bearer_token_from_cache(auth_cache: AuthCache | None = None) -> dict[str, str]:
    """
    A successful login CLI command returns a bearer token that is stored into the auth_cache and can be used to make
    authenticated calls against platform (e.g., for Cloud Pods).
    This function retrieves such a token and build the authorization header for such platform calls.
    :param auth_cache: the cache for the token.
    :return: the authorization header to authenticate against platform calls.
    """
    auth_cache = auth_cache or get_auth_cache()
    token_dict = auth_cache.get("token")
    id_token = token_dict
    if isinstance(id_token, dict):
        # refresh the auth token, if necessary
        auth_client = AuthClient()
        token_obj = AuthToken(token_dict.get("token"), metadata=token_dict)
        token_obj = auth_client.refresh_token(token_obj)

        # update token cache
        token_dict_new = token_obj.to_dict()
        if token_dict != token_dict_new:
            token_dict.update(token_dict_new)
            auth_cache["token"] = token_dict
            auth_cache.save()

        # get id token string from token object
        id_token = token_obj.token

    if id_token:
        provider = auth_cache.get("provider") or "internal"
        prefix = f"{provider} "
        if not id_token.startswith(prefix) and " " not in id_token:
            id_token = f"{prefix}{id_token}"
        return {"authorization": id_token}


def get_platform_auth_headers(auth_cache: AuthCache | None = None) -> dict[str, str]:
    """
    Creates auth headers used for connecting to the LocalStack backend (a.k.a., our platform), either a
    token bearer authorization header from the auth cache (created by a login command),
    or the environment API key.
    """
    # Try to build the authentication headers from the credentials in the environment
    credentials = get_credentials_from_environment()
    if isinstance(credentials, LSAuthToken):
        auth_token_encoded = to_str(base64.b64encode(to_bytes(f":{credentials.encoded()}")))
        return {"Authorization": f"Basic {auth_token_encoded}"}
    if isinstance(credentials, ApiKeyCredentials):
        return {"ls-api-key": credentials.encoded(), "ls-version": VERSION}

    # Try to retrieve the token from the auth cache
    if not (auth := get_bearer_token_from_cache(auth_cache or get_auth_cache())):
        raise Exception(
            "Auth token not configured! "
            "Please run 'localstack auth set-token <AUTH_TOKEN>', "
            "or set the environment variable LOCALSTACK_AUTH_TOKEN to a valid auth token."
        )
    return auth
