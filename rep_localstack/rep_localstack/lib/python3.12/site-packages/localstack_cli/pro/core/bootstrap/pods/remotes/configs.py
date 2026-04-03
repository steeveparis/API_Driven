from dataclasses import asdict, dataclass, field
from urllib.parse import quote, urlparse

DEFAULT_REMOTE_SCHEME = "platform"


@dataclass
class RemoteConfig:
    """
    Defines the configurations of a specific remote storage backend, used to perform cloud pods remote operations.

    Note: For now, the remote config only consists of a URL, which would encapsulate all the details required to
          interact with the remote (incl. auth tokens, etc). Over time, we may think of introducing a way to add
          more fine-grained configurations for remotes.

    Note: Remote URLs may contain `{..}` placeholders like `proto://{user}:{secret}@host', which will then
          get replaced with concrete user parameters specified via `RemoteConfigParams` at runtime.

    For the special case of our own remote platform (which is the default for remote operations), specifying the URL
    is not required, and the access credentials are retrieved from the token cache populated on "localstack login".
    """

    remote_url: str
    """The URL of the remote"""

    @property
    def scheme(self) -> str:
        scheme = DEFAULT_REMOTE_SCHEME
        if self.remote_url:
            scheme = urlparse(self.remote_url).scheme
        return scheme


@dataclass
class RemoteConfigParams:
    """
    Runtime parameters for cloud pod remotes, extracted from the user environment for remote operations.

    The remote parameters typically contain credentials which are specified by the user and are then rendered
    into the remote URL at runtime (to keep secret values ephemeral and avoid persisting them within URLs).
    """

    remote_name: str
    """The name of the remote"""
    remote_params: dict[str, str] = field(default_factory=dict)
    """The runtime parameters of the remote"""

    def render_url(self, remote_url: str) -> str:
        """
        Render the given remote URL template (e.g., 's3://{key}:{secret}@bucket') with the remote params.
        """
        if self.remote_params:
            remote_params_escaped = {k: quote(v or "") for k, v in self.remote_params.items()}
            remote_url = remote_url.format(**remote_params_escaped)
        try:
            # check if all placeholders have been replaced
            remote_url.format()
        except Exception as e:
            raise Exception(
                f"Missing parameters for cloud pod remote URL template: {remote_url}"
            ) from e
        return remote_url

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, params: dict) -> "RemoteConfigParams":
        return cls(**params)
