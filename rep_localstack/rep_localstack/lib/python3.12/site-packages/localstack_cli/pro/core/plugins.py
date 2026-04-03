"""
Pro plugin hooks for the LocalStack CLI.

These hooks are executed on the host machine when running CLI commands like `localstack start`.
They handle license activation, container configuration, and extension developer mode.

Note: This file was extracted from localstack-pro-core/localstack/pro/core/plugins.py
and rewritten to contain only CLI-relevant functionality.
"""

from __future__ import annotations

import logging
import os

import requests
from localstack_cli import config as localstack_config
from localstack_cli.config import HostAndPort
from localstack_cli.constants import API_ENDPOINT
from localstack_cli.pro.core import config as pro_config
from localstack_cli.pro.core.bootstrap import licensingv2
from localstack_cli.runtime import hooks
from localstack_cli.runtime.exceptions import LocalstackExit
from localstack_cli.utils.bootstrap import Container

LOG = logging.getLogger(__name__)


def modify_gateway_listen_config(cfg):
    """
    Modifies the localstack config to additionally listen to port 443.
    Needs to be called before any edge URLs are resolved using the config.
    """
    if os.getenv("GATEWAY_LISTEN") is None:
        host = "0.0.0.0" if localstack_config.in_docker() else "127.0.0.1"
        cfg.GATEWAY_LISTEN.append(HostAndPort(host=host, port=443))


@hooks.prepare_host(priority=200)
def patch_community_pro_detection():
    """This is currently needed to make localstack core aware of the `localstack auth set-token`
    functionality, where we set the key into the ``~/.localstack/auth.json`` file that community does not
    yet know about. ``is_api_key_configured`` is used in the LocalStack CLI to determine whether to start
    the localstack or localstack-pro container image."""
    from localstack_cli.utils import bootstrap

    bootstrap.is_auth_token_configured = pro_config.is_auth_token_configured


## ---------------------------------------------------------------------------
## Grace period helpers (temporary, to be removed once grace period expires)
## ---------------------------------------------------------------------------

GRACE_PERIOD_ENDPOINT_PATH = "/license/grace-period-check"

LICENSE_ERROR_MESSAGE = """\
===============================================
LocalStack requires an account to run.

==> Have an account? Learn how to set LOCALSTACK_AUTH_TOKEN: https://app.localstack.cloud/settings/auth-tokens
==> Need an account? Get started: https://www.localstack.cloud/pricing
==> Want more time? Snooze until April 6, 2026 by setting LOCALSTACK_ACKNOWLEDGE_ACCOUNT_REQUIREMENT=1
"""

GRACE_PERIOD_EXPIRED_MESSAGE = """\
===============================================
LocalStack requires an account to run.

==> Have an account? Learn how to set LOCALSTACK_AUTH_TOKEN: https://app.localstack.cloud/settings/auth-tokens
==> Need an account? Get started: https://www.localstack.cloud/pricing
"""


def _check_grace_period_active(ack: bool) -> bool:
    """Call the platform grace period endpoint.

    Returns True if the grace period is active (200 response).
    Returns False for any other response (including 404 when the endpoint is removed).
    """
    from localstack_cli.pro.core.bootstrap.licensingv2 import get_system_information_summary
    from localstack_cli.pro.core.constants import VERSION
    from localstack_cli.utils.analytics.metadata import get_client_metadata
    from localstack_cli.utils.http import get_proxies

    metadata = get_client_metadata()
    payload = {
        "machine": {
            "id": metadata.machine_id,
            "ci": metadata.is_ci,
            "system": get_system_information_summary(),
        },
        "version": VERSION,
        "requesting_grace": ack,
    }

    url = f"{API_ENDPOINT}{GRACE_PERIOD_ENDPOINT_PATH}"
    proxies = get_proxies()

    try:
        response = requests.post(
            url,
            json=payload,
            verify=not localstack_config.is_env_true("SSL_NO_VERIFY"),
            proxies=proxies,
            timeout=10,
        )
        return response.ok
    except requests.exceptions.RequestException:
        LOG.debug("Failed to reach grace period endpoint at %s", url)
        return False


## ---------------------------------------------------------------------------


@hooks.prepare_host(priority=100, should_load=pro_config.ACTIVATE_PRO)
def activate_pro_key_on_host():
    """Activate license on host (needed for DNS forward and EC2 daemon)."""
    try:
        licensingv2.get_licensed_environment().activate()
    except licensingv2.LicensingError as e:
        # license activation was unsuccessful (this can also be because no auth token was set)
        # defensively set pro to False so that we don't load pro plugins
        pro_config.ACTIVATE_PRO = False

        # check also with LOCALSTACK_ prefix here because we don't have the handling from the docker entrypoint on the host
        ack = localstack_config.is_env_true(
            "LOCALSTACK_ACKNOWLEDGE_ACCOUNT_REQUIREMENT") or localstack_config.is_env_true(
            "ACKNOWLEDGE_ACCOUNT_REQUIREMENT")
        # Note: this can also fail because they've got their connection set up wrong.
        active = _check_grace_period_active(ack)
        if not ack and active:
            # Grace period is active but unused. Prompt user to set up account or snooze.
            raise LocalstackExit(reason=LICENSE_ERROR_MESSAGE, code=55)
        if ack and active:
            # Grace period is active, start in community mode (no pro features).
            LOG.info("Grace period active: starting LocalStack in community mode")
            return
        if ack and not active:
            # Grace period expired or endpoint unreachable
            raise LocalstackExit(reason=GRACE_PERIOD_EXPIRED_MESSAGE, code=55)
        raise LocalstackExit(reason=e.get_user_friendly(), code=55)


@hooks.configure_localstack_container(priority=10, should_load=pro_config.ACTIVATE_PRO)
def configure_pro_container(container: Container):
    """Configure the LocalStack container for pro features."""
    modify_gateway_listen_config(localstack_config)
    container.configure(licensingv2.configure_container_licensing)


@hooks.prepare_host(should_load=pro_config.ACTIVATE_PRO and pro_config.EXTENSION_DEV_MODE)
def configure_extensions_dev_host():
    """Load extension directories from ~/.localstack/extensions-dev.json."""
    from localstack_cli.pro.core.bootstrap.extensions.bootstrap import run_on_configure_host_hook

    run_on_configure_host_hook()


@hooks.configure_localstack_container(
    should_load=pro_config.ACTIVATE_PRO and pro_config.EXTENSION_DEV_MODE
)
def configure_extensions_dev_container(container: Container):
    """Configure container for extension developer mode."""
    from localstack_cli.pro.core.bootstrap.extensions.bootstrap import (
        run_on_configure_localstack_container_hook,
    )

    run_on_configure_localstack_container_hook(container)
