import base64
import copy
import dataclasses
import hmac
import json
import logging
import os
import platform
import re
import textwrap
import threading
from datetime import datetime, timezone
from enum import Enum
from json import JSONDecodeError, JSONEncoder
from pathlib import PurePosixPath
from typing import Any, Protocol, cast

import dateutil.parser
from localstack_cli import config, constants
from localstack_cli.constants import VERSION
from localstack_cli.pro.core import config as pro_config
from localstack_cli.pro.core.bootstrap.entitlements import (
    ProductEntitlements,
    ProductInfo,
)
from localstack_cli.pro.core.constants import PLATFORM_PLUGIN_NAMESPACE
from localstack_cli.utils.objects import singleton_factory
from localstack_cli.utils.strings import md5
from plux import Plugin, PluginDisabled, PluginLifecycleListener, PluginSpec

LOG = logging.getLogger(__name__)

#################
#      API      #
#################


ENV_LOCALSTACK_API_KEY = "LOCALSTACK_API_KEY"
ENV_LOCALSTACK_AUTH_TOKEN = "LOCALSTACK_AUTH_TOKEN"
LICENSE_FILE_NAME = "license.json"
AWS_SERVICE_PLUGIN_NAMESPACE = "localstack.aws.provider"


class LicensingError(Exception):
    """Base exception for errors during license requests, validation, or activation."""

    default_message = None

    def __init__(self, msg: str = None, *args):
        msg = msg or self.default_message
        if msg:
            super().__init__(msg, *args)
        else:
            super().__init__(*args)

    def get_user_friendly(self) -> str:
        """
        Returns a user-friendly message for this key activation error.
        :return: a string that can be used as log output
        """
        return textwrap.dedent(
            f"""
            ===============================================
            License activation failed! 🔑❌

            Reason: {self}

            Due to this error, Localstack has quit. LocalStack pro features can only be used with a valid license.

            - Please check that your credentials are set up correctly and that you have an active license.
              You can find your credentials in our webapp at https://app.localstack.cloud.
            - If you want to continue using LocalStack without pro features, set `LOCALSTACK_ACKNOWLEDGE_ACCOUNT_REQUIREMENT=1` during the grace period.
            """
        )

    def get_user_friendly_cli(self):
        reason = "".join(textwrap.fill(str(self))).strip()
        # for reasons I don't understand and am too lazy to figure out, textwrap.dedent indents the text
        # when using a string that was formatted with textwrap.fill. so we don't use it here.
        return f"""
=============================================
You tried to use a LocalStack feature that requires a paid subscription,
but the license activation has failed! 🔑❌

Reason: {reason}

Due to this error, LocalStack has quit.

- Please check that your credentials are set up correctly and that you have an active license.
  You can find your credentials in our webapp at https://app.localstack.cloud.
- If you haven't yet, sign up on the webapp and get a free trial!
"""


class LicenseFormatError(LicensingError):
    """Indicates that something is wrong with the format of the license."""

    default_message = "A parsing error was encountered while parsing the license file or while decoding the license secret."


class LicenseRequestError(LicensingError):
    """Base exceptions for errors that occur while requesting a new license from auth credentials."""

    pass


class LicensingServerUnavailableError(LicenseRequestError):
    """Indicates that the licensing server is not available."""

    default_message = (
        f"Could not reach the LocalStack licensing server at {constants.API_ENDPOINT}. "
        "Please make sure outbound HTTPS traffic is allowed"
    )


class CredentialsTypeError(LicenseRequestError):
    pass


class CredentialsMissingError(LicensingError):
    default_message = (
        "No credentials were found in the environment. Please make sure to either set the "
        f"{ENV_LOCALSTACK_AUTH_TOKEN} variable to a valid auth token. If you are using the CLI, "
        f"you can also run `localstack auth set-token`."
    )


class CredentialsFormatError(LicenseRequestError):
    default_message = "There was an error while validating the format of the credentials defined in your environment."


class AuthTokenFormatError(CredentialsFormatError):
    default_message = (
        f"The auth token defined in {ENV_LOCALSTACK_AUTH_TOKEN} has an invalid format. "
        f"Please make sure that the environment variable {ENV_LOCALSTACK_AUTH_TOKEN} contains a valid auth "
        f"token. You can find your auth token in the LocalStack web app https://app.localstack.cloud."
    )


class CredentialsInvalidError(LicenseRequestError):
    default_message = (
        "The credentials defined in your environment are invalid. Please make sure to set the "
        f"{ENV_LOCALSTACK_AUTH_TOKEN} variable to a valid auth token. You can find your auth "
        f"token in the LocalStack web app https://app.localstack.cloud."
    )


class LicenseValidationError(LicensingError):
    """Base exception for errors that occur while trying to validate a license."""


class LicenseSignatureMismatchError(LicenseValidationError):
    """Indicates that the signature of a license did not match the calculated signature."""

    default_message = (
        "Calculated signature and license signature do not match. Please check that the "
        "credentials in your environment match the one for your license."
    )


class LicenseActivationError(LicensingError):
    """Base exception for errors that occur during license activation."""

    default_message = "The license could not be activated for unknown reasons."


class LicenseExpiredError(LicenseActivationError):
    default_message = "The license has expired."


class LicenseStaleError(LicenseActivationError):
    """Offline activation cannot be performed"""

    default_message = (
        "Offline activation failed! Your local license file that is used to activate LocalStack has "
        "expired. Please connect to the Internet and restart LocalStack to renew it."
    )

    def __init__(self, license: "License", msg=None, *args):
        super().__init__(msg, *args)
        self.license = license


class LicenseStatusError(LicenseActivationError):
    default_message = "Unexpected license status."


class LocalstackVersionMismatchError(LicenseActivationError):
    """License files are not valid across minor versions of LocalStack, even if the license allows all product
    versions."""

    default_message = (
        "The license file was created for a different LocalStack version than the one in use."
    )


class ProductMismatchError(LicenseActivationError):
    default_message = (
        "The product you are trying to activate does not match the ones in the license agreement."
    )


class Credentials:
    """
    Credentials are used to authenticate the user when requesting a license.
    """

    def to_bytes(self) -> bytes:
        raise NotImplementedError

    def encoded(self) -> str:
        raise NotImplementedError

    def is_valid(self) -> bool:
        raise NotImplementedError

    def __eq__(self, other):
        return other.to_bytes() == self.to_bytes()


class ApiKeyCredentials(Credentials):
    """
    A wrapper to use a LOCALSTACK_API_KEY as credentials.

    Deprecated in favor of LOCALSTACK_AUTH_TOKEN credentials.
    """

    api_key: str

    def __init__(self, api_key: str):
        self.api_key = api_key

    def __repr__(self):
        return f'"{self.api_key[:3]}..."({len(self.api_key)})'

    def encoded(self) -> str:
        return self.api_key

    def is_valid(self) -> bool:
        return True  # TODO add basic API key syntax validation

    def to_bytes(self) -> bytes:
        return self.api_key.encode("utf-8")


class AuthToken(Credentials):
    """
    The LOCALSTACK_AUTH_TOKEN is a 36+ character (39+ prefixed) semi-pronounceable token that looks similar
    to a UUID.
    The prefix always starts with ls and ends with a "-".
    Here are some examples and their constituent parts::

        lsci-sekU1905-PohE-0982-duXA-pUQITIxi3829
        ^^^^
        prefix

        ls-WeNE7839-VifA-viLo-LImo-SehU0277efb9
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
           payload (32 chars)

        ls-Wefu6414-YUvE-8314-ZiLA-zIlOnocAfb0d
                                           ^^^^
                                         checksum

    The last 4 digits of the token are the checksum. The checksum consists of the first 4 characters of the
    md5 hexdigest of the 32 payload characters of the token.
    """

    TOKEN_PREFIX = "ls[a-zA-Z]*-"
    MIN_TOKEN_PREFIX_LENGTH = 3

    TOKEN_REGEX = re.compile(
        rf"^{TOKEN_PREFIX}[a-zA-Z]{{4}}[a-zA-Z0-9]{{4}}-[a-zA-Z0-9]{{4}}-[a-zA-Z0-9]{{4}}-[a-zA-Z0-9]{{4}}-[a-zA-Z0-9]{{12}}$"
    )
    MIN_TOKEN_LENGTH = 36 + MIN_TOKEN_PREFIX_LENGTH

    token: str
    prefix: str
    payload: str

    def __init__(self, token: str):
        self.token = token
        # prefix is everything until the first "-"
        self.prefix, _, payload_checksum = token.partition("-")
        self.prefix += "-"
        # the payload is the part after the prefix without the last four characters
        self.payload = payload_checksum[:-4]
        # the checksum are the last four characters
        self.checksum = payload_checksum[-4:]

    def __repr__(self):
        n = 8 + len(self.prefix)  # show the first 8 characters plus the prefix
        return f"{self.token[:n]}-****-****-****-************"

    def encoded(self) -> str:
        return self.token

    def to_bytes(self) -> bytes:
        return self.token.encode("utf-8")

    def is_valid(self) -> bool:
        return self.is_syntax_valid() and self.is_checksum_valid()

    def is_syntax_valid(self) -> bool:
        if len(self.token) < self.MIN_TOKEN_LENGTH:
            return False
        return bool(self.TOKEN_REGEX.match(self.token))

    def is_checksum_valid(self) -> bool:
        """
        Checks whether the token checksum is correct. The checksum is the last 4 characters of the token and
        consists of the first 4 digits of the md5 hexdigest of the first 32 characters (without the prefix)
         of the token.

        :return: true if the checksum is valid
        """
        if len(self.token) < self.MIN_TOKEN_LENGTH:
            return False

        return md5(self.payload)[:4] == self.checksum


class LicenseStatus(Enum):
    UNKNOWN = "UNKNOWN"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"


@dataclasses.dataclass
class License:
    id: str
    """The unique ID of the license"""

    license_format: str
    """The license format version, e.g., '1'"""

    signature: str | None
    """The base64 encoded signature used to validate whether the license was signed correctly"""

    def copy(self) -> "License":
        """
        Create a deep copy of this license document. Uses ``copy.deepcopy`` by default.
        :return: a copy of this license document.
        """
        return copy.deepcopy(self)

    def to_log_string(self) -> str:
        return self.id


@dataclasses.dataclass
class LicenseV1(License):
    license_type: str
    """The license type (basically the plan: Free, Trial, Pro, Team, Enterprise, ...)"""

    issue_date: datetime
    """When the license was issued"""

    expiry_date: datetime
    """When the license expires"""

    products: list[ProductInfo] = dataclasses.field(default_factory=list)
    """The products that are subject of this license."""

    license_status: LicenseStatus = LicenseStatus.UNKNOWN
    """The license status (e.g., ACTIVE, EXPIRED, ...)."""

    license_secret: str | None = None
    """The base64 encoded secret that contains the necessary information to activate the license offline."""

    last_activated: datetime | None = None
    """The last time the user checked in"""

    reactivate_after: datetime | None = None
    """The next time a user needs to check in"""

    offline_data: dict[str, str] = dataclasses.field(default_factory=dict)
    """Additional data stored in the offline license file which is not part of the signature."""

    def to_log_string(self):
        # TODO remove this once API keys have been phased out,
        #  or when the ID a "fake" license issued for an API key is not the API key itself anymore
        # The ID of "fake" licenses for API keys is the API key itself. Redact it in that case.
        # Real licenses are "license_<uuid>", i.e. at least 44 chars long.
        prefix = self.id if len(self.id) > 30 else f"{self.id[:3]}..."
        return f"{prefix}:{self.license_type}"


class LicenseSigner:
    """
    This is (in the future) server-side code to generate license keys and sign license files.
    """

    def calculate_signature(self, license: License) -> str:
        raise NotImplementedError


class LicensingClient:
    """
    The licensing client implements the communication between LocalStack and the licensing server. The
    abstractions are used by the ``LicensedLocalstackEnvironment``. We also have a special
    ``ApiKeyLicensingClient`` that implements the "self-signed" licenses that make this code backwards
    compatible with ``LOCALSTACK_API_KEY`` (deprecated).
    """

    def request_new_license(self, credentials: Credentials) -> License:
        """
        Requests a new license from the license server using the given credentials.

        :param credentials: the entities credentials (like the auth token)
        :return: a new license
        :raises LicensingError: there was an error requesting the license
        """
        raise NotImplementedError

    def validate_license(self, credentials: Credentials, license: License):
        """
        Checks whether the license is valid (has a valid signature, was not tampered with).

        :param credentials: the credentials needed to validate the license
        :param license: the license to validate
        :raises LicenseValidationError: if the license is somehow invalid
        """
        raise NotImplementedError

    def activate_license_offline(self, credentials: Credentials, license: License):
        """
        Performs an offline license activation of the given license file that was read from a disk. This is
        purely client-side and obviously inherently trust-based.

        :param license: the license file cached on the client
        """
        raise NotImplementedError

    def activate_license_online(self, credentials: Credentials, license: License) -> License:
        """
        Performs a license activation with the licensing server and returns a new license file if successful.

        :param license: the license file cached on the client
        :return: a new license
        """
        raise NotImplementedError

#####################
# v1 implementation #
#####################


class _LicenseJsonEncoder(JSONEncoder):
    """
    Special JSONEncoder used to serialize the license file.
    """

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat(timespec="seconds")
        if isinstance(obj, bytes):
            return base64.b64encode(obj).decode("utf-8")
        if isinstance(obj, Credentials):
            return obj.encoded()
        if isinstance(obj, LicenseStatus):
            return obj.value
        return super().default(obj)


class LicenseSerializer:
    def serialize(self, license: License) -> bytes:
        if not license.license_format:
            raise LicenseFormatError("missing license_format attribute")

        if license.license_format == "1":
            # license file format 1 is simply a json doc
            doc = dataclasses.asdict(license)
            doc.pop("credentials", None)  # don't save credentials in license
            return json.dumps(doc, cls=_LicenseJsonEncoder, indent=2).encode("utf-8")

        raise LicenseFormatError(f"unknown license format version {license.license_format}")


class LicenseParser:
    def parse(self, document: bytes) -> License:
        try:
            attributes = json.loads(document)
        except JSONDecodeError as e:
            raise LicenseFormatError(f"could not de-serialize json license: {e}") from e

        try:
            if attributes.get("license_format") != "1":
                raise LicenseFormatError("unknown license format")

            date_fields = ["issue_date", "expiry_date", "reactivate_after"]
            for field in date_fields:
                attributes[field] = dateutil.parser.parse(attributes[field])

            attributes["license_status"] = LicenseStatus(attributes["license_status"])

            return LicenseV1(**attributes)
        except (KeyError, ValueError) as e:
            raise LicenseFormatError(f"error parsing license file: {e}") from e


class LicenseV1ClientBase(LicensingClient):
    def validate_license(self, credentials: Credentials, license: LicenseV1):
        try:
            now = datetime.now(tz=timezone.utc)

            if license.expiry_date < now:
                raise LicenseExpiredError()

            if license.license_status != LicenseStatus.ACTIVE:
                raise LicenseStatusError(
                    f"expected license to be ACTIVE, was {license.license_status}"
                )

            if license.reactivate_after < now:
                raise LicenseStaleError(license)

            if license_version := license.offline_data.get("localstack_version"):
                if license_version.split(".")[:2] != VERSION.split(".")[:2]:
                    raise LocalstackVersionMismatchError()
            else:
                # we also raise a LocalstackVersionMismatchError if the localstack_version field is not there,
                # this clears any old license files that don't yet have this field
                raise LocalstackVersionMismatchError()

            if LicenseSignerV1(credentials).calculate_signature(license) != license.signature:
                raise LicenseSignatureMismatchError()
        except KeyError as e:
            raise LicenseFormatError(f"missing attribute: {e}")

    def activate_license_offline(self, credentials: Credentials, license: LicenseV1):
        self.validate_license(credentials, license)

        if not self.current_product_version_matches(license):
            raise ProductMismatchError()

    def current_product_version_matches(self, license_: LicenseV1) -> bool:
        try:
            allowed_version = license_.products[0]["version"].split(".")
            current_version = VERSION.split(".")

            # FIXME: hack until we've figured out a better way to handle versions
            if license_.products[0]["version"].startswith("*"):
                return True

            vt1 = allowed_version[0], allowed_version[1]
            vt2 = current_version[0], current_version[1]
        except IndexError as e:
            raise LicenseFormatError() from e

        return vt1 == vt2


class LicenseV1Client(LicenseV1ClientBase):
    def request_new_license(self, credentials: Credentials) -> License:
        if not credentials:
            raise CredentialsMissingError()

        if not credentials.is_valid():
            raise CredentialsFormatError()

        license_ = self._request_license(credentials)
        license_.offline_data["localstack_version"] = VERSION
        return license_

    def _get_machine_data(self) -> dict:
        from localstack_cli.utils.analytics.metadata import get_client_metadata

        metadata = get_client_metadata()

        return {
            "id": metadata.machine_id,
            "cli": config.is_env_true("LOCALSTACK_CLI"),
            "ci": metadata.is_ci,
            "system": get_system_information_summary(),
        }

    def _get_product_data(self) -> dict:
        from localstack_cli.pro.core.constants import VERSION

        return {
            "name": "localstack-pro",
            "version": VERSION,
        }

    def _perform_licensing_request(self, path: str, payload: dict):
        import requests
        from localstack_cli.utils.http import get_proxies
        from localstack_cli.utils.sync import retry

        proxies = get_proxies()

        def _request():
            try:
                return requests.post(
                    f"{constants.API_ENDPOINT}{path}",
                    json.dumps(payload),
                    verify=not config.is_env_true("SSL_NO_VERIFY"),
                    proxies=proxies,
                    timeout=10,
                )
            except requests.exceptions.RequestException as e:
                raise LicensingServerUnavailableError() from e

        return retry(_request, retries=2, sleep=1)

    def activate_license_online(self, credentials: Credentials, license: LicenseV1) -> LicenseV1:
        if isinstance(credentials, AuthToken):
            credentials_type = "licensing-auth-token"
            token = credentials.token
        elif isinstance(credentials, ApiKeyCredentials):
            credentials_type = "licensing-api-key"
            token = credentials.api_key
        else:
            raise CredentialsTypeError(f"{type(credentials)}")

        response = self._perform_licensing_request(
            "/license/activate",
            {
                "license_id": license.id,
                "credentials": {
                    "type": credentials_type,
                    "token": token,
                },
                "product": self._get_product_data(),
                "machine": self._get_machine_data(),
            },
        )

        if response.ok:
            return cast(LicenseV1, LicenseParser().parse(response.content))

        self._server_error_to_exception(response)

    def _request_license(self, credentials: AuthToken | ApiKeyCredentials) -> LicenseV1:
        if isinstance(credentials, AuthToken):
            credentials_type = "licensing-auth-token"
            token = credentials.token
        elif isinstance(credentials, ApiKeyCredentials):
            credentials_type = "licensing-api-key"
            token = credentials.api_key
        else:
            raise CredentialsTypeError(f"{type(credentials)}")

        response = self._perform_licensing_request(
            "/license/request",
            {
                "credentials": {
                    "type": credentials_type,
                    "token": token,
                },
                "product": self._get_product_data(),
                "machine": self._get_machine_data(),
            },
        )

        if response.ok:
            return LicenseParser().parse(response.content)

        self._server_error_to_exception(response)

    def _server_error_to_exception(self, response):
        try:
            doc = response.json()
            if not doc.get("message"):
                raise LicensingError(f"Unexpected license server error: {response.text}")
            message = doc["message"]
        except Exception:
            raise LicensingError(f"Unexpected license server error: {response.text}")

        if message == "licensing.credentials.invalid":
            raise CredentialsInvalidError()
        if message == "licensing.credentials.format":
            raise AuthTokenFormatError()
        if message == "licensing.activation_error":
            raise LicenseActivationError()
        if message == "licensing.license.not_found":
            raise LicenseActivationError("The license with the given ID was not found.")
        if message.startswith("licensing.license.invalid_status"):
            parts = message.split(":")
            raise LicenseStatusError(f"Expected license to be ACTIVE, was {parts[1]}")
        if message == "licensing.license.expired":
            raise LicenseExpiredError()
        # TODO: complete

        else:
            raise LicensingError(f"Unexpected license server error: {response.text}")


class LicensedLocalstackEnvironment:
    """
    The LicensedLocalstackEnvironment implements a generic license activation flow over the base abstractions
    ``License``, ``LicensingClient``, ``LicenseParser`` and ``LicenseSerializer``.

    It is stateful and relates to one license that can be cached/loaded from disk
    """

    license: License | None
    """The activated license set after ``activate`` was called."""

    def __init__(
        self,
        client: LicensingClient,
        parser: LicenseParser = None,
        serializer: LicenseSerializer = None,
    ):
        self.client = client
        self.parser = parser or LicenseParser()
        self.serializer = serializer or LicenseSerializer()

        self.license = None
        self.license_file_path = None
        self._product_entitlements: ProductEntitlements | None = None
        self._activated = False
        self._mutex = threading.RLock()

    @property
    def activated(self) -> bool:
        """Returns true if the license has been activated successfully."""
        return self._activated

    def _set_license(self, license: License | None) -> None:
        self.license = license
        self._product_entitlements = None

    @property
    def product_entitlements(self) -> ProductEntitlements:
        """
        Returns the product entitlements derived from the active license document.
        """
        if not self.activated or not isinstance(self.license, LicenseV1):
            return ProductEntitlements([])

        if self._product_entitlements is None:
            self._product_entitlements = ProductEntitlements(self.license.products)

        return self._product_entitlements

    def activate(self, offline_only: bool = False):
        """
        This high-level method activates the license using credentials from the environment.
        Once completed, it sets ``self.license`` to the license document that was activated.
        This method will also set the env var ``PRO_ACTIVATED=1`` after successful license activation.

        See ``activate_license`` for the activation algorithm.

        The method is thread safe and idempotent, so you can call it multiple times and use it as guard::

            try:
                get_licensed_environment().activate()
            except LicensingError as e:
                ...

        If you want to check that the environment is activated, you can also::

            if get_licensed_environment().activated:
                ...

        :param offline_only: if set to True, no online license activation will be attempted,
        and the license activation will fail immediately if the offline activation fails.
        """
        with self._mutex:
            if self.activated:
                return

            self.activate_license(offline_only)

            os.environ[constants.ENV_PRO_ACTIVATED] = "1"

    def activate_license(self, offline_only: bool = False):
        """
        The license activation procedure works as follows:

        * Read the licensing credentials from the environment (raise an error if none are available)
        * Try to find a cached license in one of the read locations (``get_license_file_read_locations``)
        * If one was found, try to activate the license offline first.
        * If the license exists but is stale (needs to "check in" with the license server), then use the
          license id of the cached license and try to activate it.
        * If no license exists or the cached one could not be activated, try to request a new one
        * Requesting a license means, on the server, checking whether the user has an active subscription,
          and if so, check out a new license for them usable for offline activation.

        :param offline_only: whether to only attempt an offline activation
        """
        # TODO: clean up
        credentials = self.require_valid_credentials()

        license = None
        try:
            # first, try and activate one of the locally cached licenses
            try:
                result = self._try_activate_offline(credentials)

                if result and self.activated:
                    license, file_path = result

                    self._set_license(license)
                    self.license_file_path = file_path
                    LOG.info(
                        "Successfully activated cached license %s from %s 🔑✅",
                        license.to_log_string(),
                        file_path,
                    )
                    return
            except LicenseStaleError as e:
                license = e.license
                if offline_only:
                    raise

        except LicensingError as e:
            if offline_only:
                raise
            LOG.debug(
                "Attempting online activation after offline activation failed: %s:%s",
                type(e).__name__,
                e,
            )

        if license:
            try:
                license = self.client.activate_license_online(credentials, license)
                self.client.validate_license(credentials, license)
                self._activated = True
                self._set_license(license)
                LOG.info("Successfully activated license %s 🔑✅", license.to_log_string())
                self.save_license()
                return
            except LicensingError as e:
                LOG.debug(
                    "There was an error activating the license: %s. Trying to get a new one.", e
                )

        license = self.client.request_new_license(credentials)
        self.client.validate_license(credentials, license)
        self._activated = True
        self._set_license(license)
        LOG.info(
            "Successfully requested and activated new license %s 🔑✅", license.to_log_string()
        )
        self.save_license()

    def _try_activate_offline(self, credentials: Credentials) -> tuple[License, str] | None:
        errors = []
        # try to activate the license offline from license file locations one by one. some may be read-only
        for license_file_path in self.get_license_file_read_locations():
            if not os.path.isfile(license_file_path):
                continue

            try:
                with open(license_file_path, "rb") as fd:
                    license = self.parser.parse(fd.read())
                    self.client.activate_license_offline(credentials, license)
                    self._activated = True

                    return license, license_file_path
            except LicensingError as e:
                LOG.debug("Failed to activate license file %s: %s", license_file_path, e)
                errors.append(e)

        if errors:
            # raise the first error. there may be more than one license file in the locations, so we pick
            # the one with the highest priority to report.
            # TODO: one potential error could be LicenseActivationRequiredError, which should actually be the
            #  one to prefer
            raise errors[0]

        return None

    def has_product_license(self, product_name: str) -> bool:
        """
        .. deprecated::
           Use ``get_product_entitlements().has_entitlement()`` instead.

        Checks whether the given product is licensed under the currently activated license. Raises a
        LicensingError if the license hasn't been activated yet.

        The comparison uses fnmatch to compare, so the license could contain a ProductInfo record
        ``localstack.extensions/*`` which would give access to all restricted extensions.

        :param product_name: the product name to check, for example ``localstack.extensions/outages``.
        :return: true if the given product is part of the license
        """
        return self.product_entitlements.has_entitlement(product_name)


    def save_license(self):
        """
        Serializes the active license into the license file write location. When running in the container,
        this will be ``/var/lib/localstack/cache/license.json``, and in the CLI this will be the
        system-specific ``~/.cache/localstack-cli``, which we then mount into the container.

        :return:
        """
        if not self.activated:
            raise ValueError("not yet activated")
        if not self.license:
            raise ValueError("no license to save")

        file_path = self.get_license_file_write_location()

        LOG.debug("Caching license file to %s", file_path)
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            with open(file_path, "wb") as fd:
                fd.write(self.serializer.serialize(self.license))
        except OSError as e:
            LOG.debug("Error caching license file to %s: %s", file_path, e)

    def get_license_file_write_location(self) -> str:
        if config.is_env_true("LOCALSTACK_CLI"):
            # in the case of the CLI, we write to the CLI cache dir. this switch is left here on purpose to
            # make the two different control paths more and explicit clear. moreover, we should at some point
            # get rid of `config.dirs` in the CLI code.
            return os.path.join(config.dirs.cache, LICENSE_FILE_NAME)
        else:
            # in the case of an infra process, we'll write it to the cache dir
            return os.path.join(config.dirs.cache, LICENSE_FILE_NAME)

    def get_license_file_read_locations(self) -> list[str]:
        """
        Returns a list of file paths where license files may be located that can be activated offline. The
        paths may be read-only.

        :return: a list of file paths
        """
        if config.is_env_true("LOCALSTACK_CLI"):
            return [
                os.path.join(
                    config.dirs.cache, LICENSE_FILE_NAME
                ),  # this will be ~/.cache/localstack-cli
            ]
        else:
            return [
                os.path.join(
                    config.dirs.config, LICENSE_FILE_NAME
                ),  # license mountpoint from the CLI
                os.path.join(config.dirs.cache, LICENSE_FILE_NAME),
                # checking static libs can be helpful for enterprise licensing that have perpetual license
                # baked into the image
                os.path.join(config.dirs.static_libs, LICENSE_FILE_NAME),
            ]

    def require_valid_credentials(self) -> Credentials:
        """
        Returns the credentials from the environment or raises a CredentialsMissingError if they don't exist.

        :return: a credentials object
        """
        credentials = get_credentials_from_environment()

        if not credentials:
            raise CredentialsMissingError()

        if not credentials.is_valid():
            raise CredentialsInvalidError()

        return credentials


class DevLocalstackEnvironment(LicensedLocalstackEnvironment):
    """
    Special LicensedLocalstackEnvironment used when ``test`` is used as auth token or api key. This
    environment only works if you have access to the localstack-pro source code, which we assume gives you
    access to all features of localstack as well as restricted extensions.
    """

    def __init__(
        self,
        client: LicensingClient,
        parser: LicenseParser = None,
        serializer: LicenseSerializer = None,
    ):
        super().__init__(client=client, parser=parser, serializer=serializer)
        self._dev_product_entitlements = self._build_dev_entitlements()

    def activate_license(self, offline_only=False):
        LOG.debug("Using test license, skipping activation.")
        self._activated = True

    @property
    def product_entitlements(self) -> ProductEntitlements:
        return self._dev_product_entitlements

    @staticmethod
    def _build_dev_entitlements() -> ProductEntitlements:
        allow_all = pro_config.DEV_PRODUCT_ENTITLEMENTS_ALLOW_ALL

        try:
            products: list[ProductInfo] = json.loads(pro_config.DEV_PRODUCT_ENTITLEMENTS_LIST)
        except json.JSONDecodeError:
            LOG.warning("Invalid DEV_PRODUCT_ENTITLEMENTS_LIST; ignoring override")
            return ProductEntitlements([], allow_all=allow_all)

        if not products:
            return ProductEntitlements([], allow_all=allow_all)

        if not isinstance(products, list):
            LOG.warning("DEV_PRODUCT_ENTITLEMENTS_LIST must be a JSON list; ignoring override")
            return ProductEntitlements([], allow_all=allow_all)

        return ProductEntitlements(products, allow_all=allow_all)

    def get_license_file_locations(self) -> list[str]:
        return []

    def enable_decryption(self):
        raise LicenseActivationError("Cannot activate pro code when using test credentials")


class LicenseSignerV1(LicenseSigner):
    def __init__(self, credentials: Credentials):
        self.credentials = credentials

    def calculate_signature(self, license: LicenseV1) -> str:
        """
        The V1 signature is a sha256 hmac with the licensing credentials as key. The components of the
        license are then used to calculate the hmac.

        :param license: the license to sign
        :return: the hexdigest of the signature
        :raises LicenseFormatError: if the license doesn't contain the values necessary for the signature
        """
        encoding = "utf-8"

        try:
            # we are signing the license with the user's auth token, which is a bit silly of course,
            # but in the future this could be based on shared secrets or public-key crypto.
            sign_key = self.credentials.to_bytes()

            h = hmac.new(sign_key, digestmod="sha256")
            h.update(license.id.encode(encoding))

            for product in sorted(license.products, key=lambda p: (p["name"], p["version"])):
                h.update(product["name"].encode(encoding))
                h.update(product["version"].encode(encoding))

            h.update(license.license_format.encode(encoding))
            h.update(license.issue_date.isoformat(timespec="seconds").encode(encoding))
            h.update(license.expiry_date.isoformat(timespec="seconds").encode(encoding))
            h.update(license.license_type.encode(encoding))
            h.update(license.license_status.value.encode(encoding))
            if license.reactivate_after:
                h.update(license.reactivate_after.isoformat(timespec="seconds").encode(encoding))
        except (KeyError, AttributeError) as e:
            raise LicenseFormatError(f"{e}") from e
        except ValueError as e:
            raise LicenseFormatError(f"error in license attribute value: {e}") from e

        return h.hexdigest()


@singleton_factory
def get_system_information_summary() -> str:
    """
    Returns a string that contains three comma separated values: The operating system, kernel version,
    and architecture. We either use the docker socket to resolve the information, if that is not available
    we fall back ``platform.uname()``. If we're in docker and we don't have the docker socket available,
    we add ``(Container)`` to the operating system type to indicate that we don't have any additional
    information.

    Some examples:

    If the Docker socket is available:
     - Docker Desktop,5.15.90.1-microsoft-standard-WSL2,x86_64
     - Linux Mint 21.1,5.19.0-32-generic,x86_64

    If the Docker socket is not available, and we're on the host:
     - Windows,10,AMD64
     - Linux,5.19.0-32-generic,x86_64

    If the Docker socket is not available, and we're in the container:
     - Linux(Container),5.19.0-32-generic,x86_64

    :return: a string representing the system's information
    """
    try:
        # try to get the system from the docker socket
        from localstack_cli.utils.docker_utils import DOCKER_CLIENT

        system = DOCKER_CLIENT.get_system_info()

        return ",".join(
            [
                system["OperatingSystem"],
                system["KernelVersion"],
                system["Architecture"],
            ]
        )
    except Exception as e:
        print(e)
        pass

    uname = platform.uname()

    if config.is_in_docker:
        return ",".join(
            [
                f"{uname.system}(Container)",
                uname.release,
                uname.machine,
            ]
        )

    return ",".join(
        [
            uname.system,
            uname.release,
            uname.machine,
        ]
    )


def get_credentials_from_environment() -> Credentials | None:
    """
    Reads the credentials from the environment necessary to activate the localstack pro code, and returns
    an appropriate object representing the credentials. This can either be an ``AuthToken`` if
    ``LOCALSTACK_AUTH_TOKEN`` is used, or ``ApiKeyCredentials`` if ``LOCALSTACK_API_KEY`` is used. If both are
    set, a ``CredentialsFormatError`` is raised. Otherwise, None is returned.

    If there are no credentials set in the environment, but we're running in the CLI, we will also check the
    auth cache file that is populated via ``localstack auth set-token``.

    :return: a Credentials object or None
    """
    api_key = os.environ.get(ENV_LOCALSTACK_API_KEY, "").strip("'\" ")
    auth_token = os.environ.get(ENV_LOCALSTACK_AUTH_TOKEN, "").strip("'\" ")

    if not auth_token and config.is_env_true("LOCALSTACK_CLI"):
        # try and get the credentials from the auth cache
        from localstack_cli.pro.core.bootstrap.auth import get_auth_cache

        try:
            auth_token = get_auth_cache().get("LOCALSTACK_AUTH_TOKEN")
        except Exception:
            pass

    if api_key and auth_token:
        raise CredentialsFormatError(
            f"please specify either {ENV_LOCALSTACK_API_KEY} or {ENV_LOCALSTACK_AUTH_TOKEN}, not both"
        )

    if api_key:
        return ApiKeyCredentials(api_key)

    if auth_token:
        return AuthToken(auth_token)

    return None


class RequiresLicenseMarker(Protocol):
    """
    Protocol for classes that advertise whether they require a license. The field can be attached to
    Extensions which will make them go through a license check before they are loaded.
    """

    requires_license: bool


class LicensedPluginLoaderGuard(PluginLifecycleListener):
    """
    A PluginLifecycleListener that checks, after plugin initialization, if the plugin requires a license,
    and if so, that the extension can be used as part of the current license agreement.

    It's based on a field ``Plugin.requires_plugin`` that is
    """

    def __init__(self, environment: LicensedLocalstackEnvironment = None):
        self.environment = environment or get_licensed_environment()

    def on_init_after(self, plugin_spec: PluginSpec, plugin: Plugin | RequiresLicenseMarker):
        try:
            requires_license = plugin.requires_license
        except AttributeError:
            return

        if not requires_license:
            return

        product_name = f"{plugin_spec.namespace}/{plugin_spec.name}"

        if product_name not in get_product_entitlements(self.environment):
            # do not show licensing warning for platform plugins, since they are not opt-in by the user
            # TODO: remove duplication of plugin namespace
            if plugin_spec.namespace not in [
                PLATFORM_PLUGIN_NAMESPACE,
                AWS_SERVICE_PLUGIN_NAMESPACE,
            ]:
                LOG.warning(
                    "Disabled plugin %s since it is not part of the current license agreement 🔑❌",
                    product_name,
                )

            raise PluginDisabled(
                plugin_spec.namespace,
                plugin_spec.name,
                reason="This feature is not part of the active license agreement",
            )


@singleton_factory
def get_licensed_environment() -> LicensedLocalstackEnvironment:
    """
    Returns a ``LicensedLocalstackEnvironment`` singleton from the environment. If ``test`` is set as
    credentials, a ``DevLocalstackEnvironment`` will be returned.

    The environment is used to activate the license and decrypt the localstack pro code. Example::

      env = get_licensed_environment()
      try:
          # activate the license and enable code decryption
          env.activate()
      except LicensingError as e:
          # license activation failed

    :return: a LicensedLocalstackEnvironment singleton
    """
    credentials = get_credentials_from_environment()
    client = LicenseV1Client()

    if credentials and credentials.to_bytes() == b"test":
        return DevLocalstackEnvironment(client=client)

    return LicensedLocalstackEnvironment(client=client)


def get_product_entitlements(
    licensed_environment: LicensedLocalstackEnvironment = None,
) -> ProductEntitlements:
    env = licensed_environment or get_licensed_environment()
    return env.product_entitlements


def configure_container_licensing(cfg):
    """
    Configures the container with licensing information from the host.

    :param cfg: the ContainerConfiguration
    """
    from localstack_cli.utils.container_utils.container_client import BindMount

    # make sure the licensing credentials make it into the container correctly if they are defined in
    # `auth.json`
    credentials = get_credentials_from_environment()

    if isinstance(credentials, AuthToken):
        cfg.env_vars[ENV_LOCALSTACK_AUTH_TOKEN] = credentials.encoded()
    elif isinstance(credentials, ApiKeyCredentials):
        cfg.env_vars[ENV_LOCALSTACK_API_KEY] = credentials.encoded()

    # mount license file if available
    license_file = os.path.join(config.dirs.cache, LICENSE_FILE_NAME)
    if os.path.exists(license_file):
        # don't join with os.path.join since this needs to be a unix path in the container
        target = str(PurePosixPath(config.Directories.for_container().config) / LICENSE_FILE_NAME)
        cfg.volumes.add(BindMount(license_file, target, read_only=True))
