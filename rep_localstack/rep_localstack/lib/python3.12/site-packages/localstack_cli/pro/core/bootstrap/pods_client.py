import builtins
import io
import json
import logging
import os
import zipfile
from abc import ABC, abstractmethod
from functools import singledispatch
from pathlib import Path
from typing import IO, Any, Optional, TypedDict
from urllib import parse
from urllib.parse import urlparse

import click
import requests
import yaml
from click import ClickException
from localstack_cli import config, constants
from localstack_cli.cli import console
from localstack_cli.constants import APPLICATION_JSON, HEADER_CONTENT_TYPE, LOCALHOST_HOSTNAME
from localstack_cli.pro.core.bootstrap import auth
from localstack_cli.pro.core.bootstrap.auth import get_platform_auth_headers
from localstack_cli.pro.core.bootstrap.pods.constants import INTERNAL_REQUEST_PARAMS_HEADER
from localstack_cli.pro.core.bootstrap.pods.remotes.api import CloudPodsRemotesClient
from localstack_cli.pro.core.bootstrap.pods.remotes.configs import (
    DEFAULT_REMOTE_SCHEME,
    RemoteConfig,
    RemoteConfigParams,
)
from localstack_cli.pro.core.bootstrap.pods.remotes.params import get_remote_params_callable
from localstack_cli.pro.core.config import CLI_INJECT_POD_IDENTITY, POD_LOAD_CLI_TIMEOUT
from localstack_cli.pro.core.constants import (
    API_PATH_PODS,
    CLOUDPODS_METADATA_FILE,
    HEADER_POD_SECRET,
)
from localstack_cli.utils.bootstrap import in_ci
from localstack_cli.utils.http import safe_requests
from localstack_cli.utils.strings import to_str
from packaging import version
from requests.structures import CaseInsensitiveDict
from rich.progress import Progress

LOG = logging.getLogger(__name__)

# header constants
HEADER_LS_API_KEY = "ls-api-key"
HEADER_LS_VERSION = "ls-version"
HEADER_AUTHORIZATION = "Authorization"


DiffResult = dict[str, list[dict[str, Any]]]
"""Maps a service to a list of localstack.pro.core.persistence.pods.diff.models.Operation"""


class CloudPodNotFound(Exception):
    def __init__(self, pod_name: str) -> None:
        super().__init__(f"Cloud pod '{pod_name}' not found")


class PodInfo(TypedDict, total=False):
    """A set of info about a pod. Mostly used to display information on the CLI."""

    name: str
    pod_id: str
    version: int
    services: list[str]
    description: str
    size: int
    remote: str
    localstack_version: str
    encrypted: bool


def fetch_state_response_from_instance(
    services: list[str] | None = None,
) -> tuple[requests.Response, PodInfo]:
    url = f"{get_runtime_pods_endpoint()}/state"
    params = ",".join(services) if services else ""

    # TODO: temporary fix to bypass Gateway 503 responses on shutdown
    headers = {INTERNAL_REQUEST_PARAMS_HEADER: "{}"}

    is_ci: bool = in_ci()
    result = requests.get(url, params={"services": params}, headers=headers, stream=not is_ci)
    if not result.ok:
        raise Exception(
            f"An error occurred while retrieving the LocalStack state (code {result.status_code})"
        )

    metadata = PodInfo(
        services=result.headers.get("x-localstack-pod-services", "").split(","),
        size=int(result.headers.get("x-localstack-pod-size", 0)),
    )

    return result, metadata


def write_state_zip_to_file(
    open_file: IO[bytes], services: list[str] | None = None, chunk_size: int = 100_000
) -> PodInfo:
    """
    Retrieves the state from LocalStack by calling the container endpoint (/_localstack/pods/state).

    Extract this state from LocalStack but write straight to a file object rather than buffering
    in memory.

    This specifically helps the state export command which writes to a local file, and enables it
    to handle arbitrary file sizes.

    :param open_file: open file object to write the bytes to
    :param services: specify a list of services for which we want to extract the in-memory state;
                     if None, it extracts all
    :param chunk_size: optional chunk size for the download (bytes)
    :return: a PodInfo object containing the metadata for the pod
    """
    result, metadata = fetch_state_response_from_instance(services)
    content_length = int(result.headers.get("Content-Length", 0))

    with Progress() as progress:
        load_task = progress.add_task("Retrieving state from the container", total=content_length)
        for chunk in result.iter_content(chunk_size=chunk_size):
            open_file.write(chunk)
            progress.update(load_task, advance=len(chunk))

    return metadata


def get_state_zip_from_instance(services: list[str] | None = None) -> tuple[bytes, PodInfo]:
    """
    Retrieves the state from LocalStack by calling the container endpoint (/_localstack/pods/state).

    :param services: specify a list of services for which we want to extract the in-memory state; if None, it extracts
                     all.

    :return: a tuple with the bytes content and a PodInfo object containing a set of metadata.
    """
    result, metadata = fetch_state_response_from_instance(services)
    content_length = int(result.headers.get("Content-Length", 0))
    is_ci: bool = in_ci()
    content = result.content if is_ci else b""

    with Progress() as progress:
        load_task = progress.add_task("Retrieving state from the container", total=content_length)
        for chunk in result.iter_content(chunk_size=100_000):
            content += chunk
            progress.update(load_task, advance=len(chunk))

    return content, metadata


class CloudPodRemoteAttributes(TypedDict, total=False):
    """A set of attributes for a Cloud Pod stored in a remote storage."""

    is_public: bool
    description: str | None
    services: list[str] | None


class PodSaveRequest(TypedDict, total=False):
    """The payload for a pod save request"""

    remote: dict[str, str | dict] | None
    attributes: CloudPodRemoteAttributes | None


class CloudPodsService(ABC):
    """Service Interface for the Cloud Pods Operations."""

    @abstractmethod
    def save(
        self,
        pod_name: str,
        attributes: CloudPodRemoteAttributes | None = None,
        remote: RemoteConfigParams | None = None,
        local: bool = False,
        # TODO: consider defining version as str rather than int
        version: int | None = None,
        secret: str | None = None,
    ) -> PodInfo:
        """
        Saves a new version of a Cloud Pod to a given storage (by default, our platform). In the process, it also
        creates a version on the pods' fyle system.
        :param pod_name: the name of the Cloud Pod
        :param attributes: a set of attributes for the Cloud Pod
        :param remote: the name/parameters of the remote storage. The default None equals to our platform
        :param local: flag that prevent the registration of the pod with the remote (it only creates the version
            locally). Mostly used for testing purposes.
        :param version: the version of the pod to save (creates a new version if not specified)
        :param secret: secret for encryption of the cloud pod content.
        """

    @abstractmethod
    def delete(
        self,
        pod_name: str,
        remote: RemoteConfigParams | None = None,
        delete_from_remote: bool = True,
    ) -> None:
        """
        Delete a Cloud Pod from the pods' file system and the remote storage.
        :param pod_name: the name of the Cloud Pod
        :param remote: the name/parameters of the remote storage. The default None equals to our platform
        :param delete_from_remote: delete the pod from the remote storage. `False` would only delete the pod cache from
            the local filesystem
        """

    @abstractmethod
    def load(
        self,
        pod_name: str,
        remote: RemoteConfigParams | None = None,
        version: int | None = None,
        merge_strategy: str | None = None,
        ignore_version_mismatches: bool = True,
        secret: str | None = None,
    ) -> None:
        """
        Loads a Cloud Pod into the LocalStack running instance. Depending on whether the latest pod's version is
        available locally or not, it will also download it from the remote storage.
        :param pod_name: the name of the Cloud Pod
        :param remote: the name/parameters of the remote storage. The default None equals to our platform
        :param version: the version of the Cloud Pod to load
        :param merge_strategy: the merge strategy to use when loading the pod
        :param ignore_version_mismatches: automatically approve the pod loading without any check or user confirmation
        :param secret: eventual encryption key (for enterprise users)
        """

    @abstractmethod
    def list(self, remote: RemoteConfigParams | None = None) -> list:
        """
        List all the Cloud Pods available in the remote storage and in the pods' file system.
        :param remote: the name/parameters of the remote
        :return: a list of Cloud Pods
        """

    @abstractmethod
    def get_versions(
        self, pod_name: str, remote: RemoteConfigParams | None = None
    ) -> builtins.list:
        """
        List all the versions of a Cloud Pod available in the remote storage.
        :param pod_name: the name of the Cloud Pod
        :param remote: the name/parameters of the remote storage. The default None equals to our platform
        :return: a list of version for the given pod
        """

    def diff(
        self,
        pod_name: str,
        remote: RemoteConfigParams | None = None,
        version: int | None = None,
    ) -> DiffResult:
        """
        Return a list of operations that represent a diff between a state stored in a Cloud Pod and the one on the
        runtime.
        :param pod_name: the name of the Cloud Pod to run the diff with.
        :param version: the version of the Cloud Pod to run the diff with.
        :param remote: the name/parameters of the remote storage.
        :return: a map between service and the list of operations representing a diff
        """

    def _get_cloud_pods_info(self, pod_name: str) -> dict:
        """
        This utility function make a call to platform and return the model object, i.e., a Cloud Pod entity.
        todo: start exporting models from the platform to handle these cases
        """
        response = requests.get(create_platform_url(pod_name), headers=get_platform_auth_headers())
        if response.status_code == 404:
            raise CloudPodNotFound(pod_name)
        if not response.ok:
            _raise_exception_with_formatted_message(
                f"Unable to get info for pod: {pod_name}", response
            )
        return json.loads(response.content)

    def _get_localstack_pod_version(
        self,
        pod_name: str,
        cloud_pods_dict: dict,
        version: int | None = None,
    ) -> str | None:
        """Returns the LocalStack version originating the Cloud Pod"""
        versions: list[dict] = cloud_pods_dict["versions"]
        max_version = int(cloud_pods_dict["max_version"])
        if version and version > max_version:
            raise Exception(
                f"Unable to load pod {pod_name} with version {version}. The maximum version available in the"
                f" remote storage is {max_version}"
            )
        # todo: generate some model to handle platform responses?
        matching_versions = list(filter(lambda v: v["version"] == version, versions))
        my_version = matching_versions[0] if matching_versions else versions[-1]
        ls_pod_version: str | None = my_version["localstack_version"]
        return ls_pod_version

    def get_state_data(self) -> dict:
        """
        Gets a representation of the service data in the running LocalStack instance.
        """
        response = requests.get(
            url=f"{get_runtime_pods_endpoint()}/state/metamodel", headers=_get_headers()
        )
        if not response.ok:
            _raise_exception_with_formatted_message(
                "Unable to get state data from the LocalStack instance", response
            )
        return json.loads(response.content)

    def set_remote_attributes(  # noqa
        self,
        pod_name: str,
        attributes: CloudPodRemoteAttributes,
        remote: RemoteConfigParams | None = None,
    ) -> None:
        """
        Updates a set of attributes for a Cloud Pod stored in a remote storage.
        :param pod_name: the name of the Cloud Pod
        :param attributes: the attributes to update
        :param remote: the name/parameters of the remote storage. The default None equals to our platform
        """
        if remote:
            LOG.debug(
                "Trying to set attributes for remote '%s'. Currently we support attributes "
                "only for the default remote",
                remote,
            )
            return
        url = create_platform_url(pod_name)
        auth_header = auth.get_platform_auth_headers()
        response = safe_requests.patch(
            url, headers=auth_header, json={"is_public": attributes["is_public"]}
        )
        if not response.ok:
            raise Exception(
                f"Error setting remote attributes for Cloud Pod {pod_name} (code {response.status_code}): "
                f"{response.text}"
            )


def _get_headers() -> dict[str, str]:
    """Returns the headers for the HTTP request with the content type and the bearer token if available."""
    headers = {HEADER_CONTENT_TYPE: APPLICATION_JSON}
    # If this flag is enabled, we inject the authorization header via the API.
    #   This header will be used to authenticate platform calls to the platform from within the container.
    #   When they are not provided, the header will be constructed from the credentials used to start the pro
    #   container. This mechanism allows to detach, if needed, container identity from pod identity.
    if CLI_INJECT_POD_IDENTITY:
        headers.update(CaseInsensitiveDict(auth.get_platform_auth_headers()))
    return headers


def _raise_exception_with_formatted_message(message: str, response: requests.Response):
    """Raises an exception with a formatted message that includes the response status code and text."""
    raise Exception(f"{message}: {response.text}")


def _get_remote_params_payload(remote: RemoteConfigParams | None) -> PodSaveRequest:
    """Uses the remote plugins in the client to get the remote parameters."""
    if not remote:
        return {}

    remote_config = _get_remote_configuration(remote, render_params=False)
    remote_params_callable = get_remote_params_callable(remote_config.remote_url)
    if not remote_params_callable:
        return {}

    remote.remote_params = remote_params_callable()
    return {"remote": remote.to_dict()}


class CloudPodsClient(CloudPodsService):
    """
    Client implementation of the Cloud Pods Service. It is a thin client that makes HTTP calls to the Cloud Pods
    endpoints in the container.
    """

    def __init__(self, interactive: bool = False) -> None:
        self.interactive = interactive

    def _process_response(self, response, message: str):
        status = console.status(message)
        status.start()

        for event in response.iter_lines():
            _event = json.loads(event)
            if _event["event"] == "log":
                status.update(_event["message"])
            if _event["event"] == "service":
                service, result, operation = (
                    _event["service"],
                    _event["status"],
                    _event["operation"],
                )
                message = (
                    f"{service}: {operation} succeeded"
                    if result == "ok"
                    else f"{service}: {operation} failed"
                )
                status.update(message)
            elif _event["event"] == "completion":
                status.stop()
                if _event["status"] == "error":
                    raise Exception(_event.get("message"))
                if _event["operation"] == "save":
                    pod_info = PodInfo(**_event["info"])
                    return pod_info
        status.stop()

    def save(
        self,
        pod_name: str,
        attributes: CloudPodRemoteAttributes | None = None,
        remote: RemoteConfigParams | None = None,
        local: bool = False,
        version: int | None = None,
        secret: str | None = None,
    ) -> PodInfo:
        url = f"{get_runtime_pods_endpoint(secret)}/{pod_name}?"
        if local:
            url += "&local=true"
        if version:
            url += f"&version={version}"
        payload: PodSaveRequest = _get_remote_params_payload(remote)
        payload.update({"attributes": attributes})
        headers = _get_headers()
        if secret:
            headers[HEADER_POD_SECRET] = secret

        response = requests.post(url=url, json=payload, headers=headers, stream=self.interactive)
        if not response.ok:
            _raise_exception_with_formatted_message(f"Unable to save pod {pod_name}", response)

        pod_info = {}
        if self.interactive:
            pod_info = self._process_response(response, message=f"Saving Cloud Pod {pod_name}")
        else:
            for line in response.iter_lines():
                line = json.loads(line)
                if line["event"] == "pod_info":
                    pod_info = PodInfo(**line["extra"])
                elif line["event"] == "exception":
                    raise Exception(line["message"])
        return pod_info

    def delete(
        self,
        pod_name: str,
        remote: RemoteConfigParams | None = None,
        delete_from_remote: bool = True,
    ):
        url = f"{get_runtime_pods_endpoint()}/{pod_name}"
        if not delete_from_remote:
            url += "?local=true"
        payload = _get_remote_params_payload(remote)
        response = requests.delete(url=url, json=payload, headers=_get_headers())
        if not response.ok:
            _raise_exception_with_formatted_message(
                f"Unable to delete Cloud Pod '{pod_name}'", response
            )

    def load(
        self,
        pod_name: str,
        remote: RemoteConfigParams | None = None,
        version: int | None = None,
        merge_strategy: str | None = None,
        ignore_version_mismatches: bool = False,
        secret: str | None = None,
    ) -> None:
        # If we are in CI, we do not ask for a confirmation
        if in_ci():
            ignore_version_mismatches = True
        remote_config = _get_remote_configuration(remote, render_params=False) if remote else None
        is_custom_remote = remote_config and remote_config.scheme != DEFAULT_REMOTE_SCHEME

        # At the moment, we do not store the information about the originating LocalStack version for additional
        # remotes. Therefore, we skip here the compatibility check.
        if not is_custom_remote and not ignore_version_mismatches:
            pod_info = self._get_cloud_pods_info(pod_name)
            ls_pod_version: str = self._get_localstack_pod_version(
                pod_name=pod_name, version=version, cloud_pods_dict=pod_info
            )
            ls_version: str = get_ls_version_from_health()
            if not is_compatible_version(ls_pod_version, ls_version) and not click.confirm(
                f"This Cloud Pod was created with LocalStack {ls_pod_version} but you are running "
                f"LocalStack {ls_version}. Cloud Pods might be incompatible across different LocalStack versions.\n"
                f"Loading a Cloud Pod with mismatching version might lead to a corrupted state of the emulator. "
                f"Do you want to continue?"
            ):
                raise click.Abort("LocalStack version mismatch")

        url = f"{get_runtime_pods_endpoint()}/{pod_name}"
        query_args = {}
        if version:
            query_args["version"] = version
        if merge_strategy:
            query_args["merge"] = merge_strategy
        if query_args:
            url += f"?{parse.urlencode(query_args)}"

        payload = _get_remote_params_payload(remote)
        headers = _get_headers()
        if secret:
            headers[HEADER_POD_SECRET] = secret
        response = requests.put(url=url, json=payload, headers=headers, stream=self.interactive)
        if not response.ok:
            _raise_exception_with_formatted_message(f"Unable to load pod {pod_name}", response)

        if self.interactive:
            self._process_response(response, message=f"Loading Cloud Pod {pod_name}")

    def list(self, remote: RemoteConfigParams | None = None, creator: str | None = None) -> list:
        payload = _get_remote_params_payload(remote)
        url = get_runtime_pods_endpoint()
        if creator:
            url += f"?creator={creator}"
        response = requests.get(url, json=payload, headers=_get_headers())
        if not response.ok:
            _raise_exception_with_formatted_message("Unable to list Cloud Pods", response)
        return json.loads(response.content).get("cloudpods", [])

    def get_versions(
        self, pod_name: str, remote: RemoteConfigParams | None = None
    ) -> builtins.list:
        """
        Returns the list of versions for a cloud pod on a given remote
        :param pod_name: the name of the Cloud Pod
        :param remote: the name of the remote storage. None equals to our platform
        """
        payload = _get_remote_params_payload(remote)
        response = requests.get(
            url=f"{get_runtime_pods_endpoint()}/{pod_name}/versions",
            json=payload,
            headers=_get_headers(),
        )
        if response.status_code == 404:
            raise Exception(f"Cloud Pod {pod_name} not found")
        if not response.ok:
            _raise_exception_with_formatted_message(
                f"Unable to get versions for pod {pod_name}", response
            )
        return json.loads(response.content).get("versions", [])

    def diff(
        self,
        pod_name: str,
        remote: RemoteConfigParams | None = None,
        version: int | None = None,
    ) -> DiffResult:
        url = f"{get_runtime_pods_endpoint()}/{pod_name}/diff"
        query_args = {}
        if version:
            query_args["version"] = version
            url += f"?{parse.urlencode(query_args)}"
        payload = _get_remote_params_payload(remote)
        response = requests.get(
            url=url,
            json=payload,
            headers=_get_headers(),
        )
        if not response.ok:
            _raise_exception_with_formatted_message(
                f"Unable to get diff for pod {pod_name}", response
            )
        return json.loads(response.content)


def _get_remote_configuration(
    params: RemoteConfigParams, render_params: bool = True
) -> RemoteConfig:
    """
    Get the remote configuration given a remote URL.
    :param params: remote parameters
    :param render_params: whether to render the remote parameters into the remote URL
    """
    remotes_client = CloudPodsRemotesClient()
    try:
        remote = remotes_client.get_remote(name=params.remote_name)
    except Exception as e:
        raise ClickException(
            f"Error getting configuration for the remote {params.remote_name}"
        ) from e
    remote_url = remote["remote_url"]
    if render_params:
        remote_url = params.render_url(remote_url)
    LOG.debug("Remote configuration: %s", remote)
    return RemoteConfig(remote_url=remote_url)


# ------------
# CLI HELPERS
# ------------


def get_runtime_pods_endpoint(passphrase: str | None = None) -> str:
    """
    Returns the runtime endpoint for the pods api.
    If a passphrase is provided, we enforce HTTPS. Moreover, we resolve the hostname and warn the user if LocalStack
    is not running locally.
    """
    if not passphrase:
        return f"{config.external_service_url()}{API_PATH_PODS}"

    edge_url = config.external_service_url(protocol="https")
    if LOCALHOST_HOSTNAME not in edge_url:
        LOG.warning(
            "LocalStack is not running locally and we are sending the encryption passphrase to a different host!"
        )
    return f"{edge_url}{API_PATH_PODS}"


class StateService:
    """Service for the local operations on Cloud Pods, i.e., exporting and importing a Cloud Pod to/from host."""

    def export_pod(self, target: str, services: Optional[list[str]] = None) -> PodInfo:  # noqa
        """
        Export a Cloud Pod to the local file system. It does not export the product space (i.e., objects and YAML files
        with information about the versions). It will save a zip file named as the pod's name in the given directory
        (if existing).
        :param target: the path on the local disk where the Cloud Pod will be saved
        """

        # check the path
        _path = urlparse(target)
        path: str = os.path.abspath(os.path.join(_path.netloc, _path.path))
        parent_path = Path(path).parent.absolute()
        if not os.path.exists(parent_path):
            raise Exception(f"{parent_path} is not a valid path")

        with open(path, "wb") as outfile:
            metadata = write_state_zip_to_file(outfile, services)

        yaml_dict: dict = get_environment_metadata()
        yaml_dict["name"] = os.path.basename(target)
        yaml_dict.update(metadata)

        # add YAML file with some metadata inside the zip
        with zipfile.ZipFile(file=path, mode="a") as zip_file:
            zip_file.writestr(CLOUDPODS_METADATA_FILE, yaml.dump(yaml_dict))
        return metadata

    def import_pod(self, source: str, show_progress: bool = True) -> None:  # noqa
        """
        Load a Cloud Pod from the local file system into the LocalStack running instance.
        :param source: the path on the local disk where the Cloud Pod is saved
        :param show_progress: flag to show visual progress in the CLI;
        """
        url = urlparse(source)
        path = os.path.abspath(os.path.join(url.netloc, url.path))
        if not os.path.exists(path):
            raise Exception(f"Path {path} does not exist")
        if not os.path.isfile(path):
            raise Exception(f"Path {path} is not a file")
        with open(path, mode="rb") as infile:
            with zipfile.ZipFile(infile, "r") as zip_file:
                yaml_metadata = read_metadata_from_pod(zip_file) or {}

            services = yaml_metadata.get("services", [])
            is_licensed_user: bool = get_environment_metadata().get("pro")
            # we check if the Cloud Pod has been generated by a pro user, and we are trying to load it as a community one
            if yaml_metadata.get("pro", False) and not is_licensed_user:
                console.print(
                    "Warning: You are trying to load a Cloud Pod generated with a Pro license."
                    "The loaded state might be incomplete."
                )
            infile.seek(0)
            load_local_state_from_open_zipfile(
                file=infile, number_services=len(services), show_progress=show_progress
            )


def list_public_pods() -> list[str]:
    """List all the public pods"""
    url = create_platform_url("public")
    auth_header = auth.get_platform_auth_headers()
    response = safe_requests.get(url, headers=auth_header)
    if not response.ok:
        raise Exception(to_str(response.content))
    content = json.loads(response.content)
    return [pod["pod_name"] for pod in content]


#####################
# Utility functions #
#####################


@singledispatch
def read_metadata_from_pod(zip_file: zipfile.ZipFile) -> dict | None:
    try:
        yaml_metadata = yaml.safe_load(zip_file.read(CLOUDPODS_METADATA_FILE))
        return yaml_metadata
    except KeyError:
        LOG.debug("No %s file in the archive", CLOUDPODS_METADATA_FILE)


@read_metadata_from_pod.register(bytes)
def _(zip_file: bytes) -> dict | None:
    zip_file = zipfile.ZipFile(io.BytesIO(zip_file), "r")
    return read_metadata_from_pod(zip_file)


@read_metadata_from_pod.register(str)
def _(zip_file: str) -> dict | None:
    with zipfile.ZipFile(zip_file) as zp:
        return read_metadata_from_pod(zp)


def call_post_load_endpoint(content: bytes | IO[bytes], stream: bool) -> requests.Response:
    """Calls the endpoint to load a state into LocalStack.
    :param content: the content to load
    :param stream: flag for a chunked response
    :return the response from the endpoint, if the status is ok.
    :raise an Exception if a non-ok status is returned
    """
    url = get_runtime_pods_endpoint()
    try:
        response = requests.post(url, data=content, timeout=POD_LOAD_CLI_TIMEOUT, stream=stream)
        if not stream:
            LOG.debug("Loaded services from local state file: %s", response.content)
    except requests.exceptions.Timeout as e:
        raise Exception(
            "Timeout exceed for the pod load operation. To avoid this issue, try to increase the"
            "value of the POD_LOAD_CLI_TIMEOUT configuration variable."
        ) from e
    if not response.ok:
        raise Exception(f"Unable to load LocalStack state via {url}")
    return response


def load_state_with_progress_bar(content: bytes | IO[bytes], number_services: int = 0) -> None:
    result = call_post_load_endpoint(content, stream=True)
    i = 0
    with Progress() as progress:
        load_task = progress.add_task("Loading state", total=number_services)
        for line in result.iter_lines():
            content = json.loads(line)
            LOG.debug("Loaded service: %s", content)
            service, status = content["service"], "✅" if content["status"] else "❌"
            progress.log(f"{service}: {status}")
            i += 1
            progress.update(load_task, completed=i)


def load_local_state(content: bytes, number_services: int = 0, show_progress: bool = True) -> None:
    """
    Call to the _localstack/pods endpoint to inject a Cloud Pod into the system.
    :param number_services: the number of services contained in the state to load. Used to display a progress bar.
    :param content: the content of the Cloud Pod to be loaded into LocalStack.
    :param show_progress: flag to show visual progress in the CLI.
    :raises Exception: if the call to the endpoint does not return an ok status code (less than 400) or if a timeout
        exceeds.
    """
    if show_progress:
        load_state_with_progress_bar(content=content, number_services=number_services)
    else:
        call_post_load_endpoint(content=content, stream=False)


def load_local_state_from_open_zipfile(
    file: IO[bytes], number_services: int = 0, show_progress: bool = False
) -> None:
    if show_progress:
        load_state_with_progress_bar(content=file, number_services=number_services)
    else:
        call_post_load_endpoint(content=file, stream=False)


def get_environment_metadata() -> dict:
    endpoint = get_runtime_pods_endpoint()
    url = f"{endpoint}/environment"
    result = requests.get(url)
    if not result.ok:
        raise Exception(f"Unable to retrieve environment metadata from {url}")
    return json.loads(result.content)


# ---------------
# UTIL FUNCTIONS
# ---------------
def reset_state(services: list = None) -> None:
    """
    Call the reset endpoint in the persistence plugin to reset the LocalStack container.
    If a list of services is provided, it calls POST _localstack/state/<service>/reset for each service.
    It not, it calls POST _localstack/state/reset that reset the entire LocalStack instance.

    :param services: the subset of services to reset (optional)
    """

    def _call_reset(_url: str) -> None:
        response = requests.post(_url)
        if not response.ok:
            LOG.debug("Reset call to %s failed: status code %s", _url, response.status_code)
            raise Exception("Failed to reset LocalStack")

    if not services:
        url = f"{config.external_service_url()}/_localstack/state/reset"
        _call_reset(url)
        return

    for service in services:
        url = f"{config.external_service_url()}/_localstack/state/{service}/reset"
        _call_reset(url)


def get_ls_version_from_health() -> str:
    """Returns the localstack ext version fetched from the health end point."""
    try:
        health_url = f"{config.external_service_url()}/_localstack/health"
        health_response = requests.get(health_url).json()
        return health_response["version"]
    except Exception:
        return ""


def create_platform_url(path: str | None = None, api_endpoint: str | None = None) -> str:
    api_endpoint = api_endpoint or constants.API_ENDPOINT
    base_url = f"{api_endpoint}/cloudpods"
    if not path:
        return base_url
    path = path if path.startswith("/") else f"/{path}"
    return f"{base_url}{path}"


def is_compatible_version(version_one: str | None, version_two: str | None) -> bool:
    """Check if two given version are equal up to the patch level."""

    # if we cannot parse the version, we assume that it is not compatible
    if not version_one or not version_two:
        return False
    v1 = version.parse(version_one)
    v2 = version.parse(version_two)
    return v1.base_version == v2.base_version
