import json
from abc import ABC, abstractmethod

import requests
from localstack_cli import config
from localstack_cli.pro.core.bootstrap.pods.constants import INTERNAL_REQUEST_PARAMS_HEADER
from localstack_cli.pro.core.constants import API_PATH_PODS


class CloudPodsRemotesInterface(ABC):
    """Service interface for the remote CRUD operations."""

    @abstractmethod
    def create_remote(self, name: str, protocols: list[str], remote_url: str | None = None) -> None:
        """Creates a new remote with a given name.
        :param name: The name of the remote.
        :param protocols: The protocols supported by the remote.
        :param remote_url: The URL of the remote.
        TODO: think about security: we should check if the URL is either (1) localhost, or (2) using HTTPS.
        """

    @abstractmethod
    def delete_remote(self, name: str) -> None:
        """Deletes a named remote."""

    @abstractmethod
    def get_remote(self, name: str) -> dict[str, str]:
        """Returns a named remote."""

    @abstractmethod
    def get_remotes(self) -> list[dict[str, str]]:
        """Returns a list of all remotes."""


class CloudPodsRemotesClient(CloudPodsRemotesInterface):
    @property
    def endpoint(self):
        return f"{config.external_service_url()}{API_PATH_PODS}/remotes"

    def create_remote(self, name: str, protocols: list[str], remote_url: str | None = None) -> None:
        params = {"name": name, "protocols": protocols, "remote_url": remote_url}
        response = self._client.post(
            url=f"{self.endpoint}/{name}",
            data=json.dumps(params),
            headers={"Content-Type": "application/json"},
        )
        if not response.ok:
            raise Exception(f"Failed to create remote: {response.content}")

    def delete_remote(self, name: str) -> None:
        response = self._client.delete(url=f"{self.endpoint}/{name}")
        if not response.ok:
            raise Exception(f"Failed to delete remote: {response.content}")

    def get_remotes(self) -> list[dict[str, str]]:
        response = self._client.get(url=self.endpoint)
        if not response.ok:
            raise Exception(f"Failed to get list of remotes: {response.content}")
        remotes = json.loads(response.content)
        return remotes.get("remotes", [])

    def get_remote(self, name: str) -> dict[str, str]:
        response = self._client.get(url=f"{self.endpoint}/{name}")
        if not response.ok:
            raise Exception(f"Failed to get remote: {response.content}")
        remote = json.loads(response.content)
        return remote

    @property
    def _client(self) -> requests.Session:
        """Return an HTTP client session, adding default headers for all requests, as required"""
        session = requests.Session()
        # TODO: temporary fix to bypass Gateway 503 responses on LocalStack container shutdown
        session.headers.update({INTERNAL_REQUEST_PARAMS_HEADER: "{}"})
        return session
