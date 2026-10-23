"""Every setting the SDK reads from the environment, resolved in one place.

Each client takes an optional `environ=` mapping and wraps it in an `Environment`; without one it reads
`os.environ` at the moment a setting is needed. Explicit constructor arguments always win.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from runsite._errors import ConfigurationError, api_error_from_response, email_error_from_response
from runsite._http import RequestSettings

DEFAULT_BASE_URL = "https://api.runsite.app"
DEFAULT_EMAIL_URL = "https://api.runsite.app/emails"
DEFAULT_S3_REGION = "auto"
API_PREFIX = "/api/v1"
API_KEY_PREFIX = "ak_live_"
API_KEY_HINT = "Create an API key in the Runsite dashboard (Settings → API keys) and set RUNSITE_API_KEY."
EMAIL_KEY_HINT = "Connect an email service to this web service in the Runsite dashboard, or pass api_key=..."
SERVICE_ID_HINT = "It is set automatically inside Runsite containers; elsewhere pass service_id=..."
S3_CREDENTIALS_HINT = "Connect a bucket to this web service in the Runsite dashboard, or pass access_key=..."
BUCKET_HINT = "Pass the bucket name or connect a bucket to this service."


class Environment:
    """Environment variables as the SDK sees them: an empty value counts as unset."""

    def __init__(self, source: Mapping[str, str] | None = None) -> None:
        self._source = source

    def mapping(self) -> Mapping[str, str]:
        return self._source if self._source is not None else os.environ

    def get(self, *names: str) -> str | None:
        """Return the first non-empty variable among `names`."""
        source = self.mapping()
        for name in names:
            value = source.get(name)
            if value:
                return value
        return None

    def missing(self, *names: str) -> list[str]:
        """The names among `names` that are unset or empty, in the given order."""
        return [name for name in names if self.get(name) is None]

    def require(self, explicit: str | None, *names: str, hint: str) -> str:
        """Return `explicit` or the first set variable, or raise naming the primary variable."""
        value = explicit or self.get(*names)
        if not value:
            raise ConfigurationError(f"{names[0]} is not set. {hint}".strip())
        return value


def platform_settings(
    environment: Environment, *, api_key: str | None, base_url: str | None, timeout: float, max_retries: int
) -> RequestSettings:
    """Settings for calls to the Runsite platform API (/api/v1)."""
    key = environment.require(api_key, "RUNSITE_API_KEY", hint=API_KEY_HINT)
    if not key.startswith(API_KEY_PREFIX):
        raise ConfigurationError(f"RUNSITE_API_KEY must be a platform API key starting with '{API_KEY_PREFIX}'.")
    root = base_url or environment.get("RUNSITE_BASE_URL") or DEFAULT_BASE_URL
    return RequestSettings(
        base_url=f"{root.rstrip('/')}{API_PREFIX}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
        max_retries=max_retries,
        error_factory=api_error_from_response,
    )


def email_settings(
    environment: Environment, *, api_key: str | None, endpoint: str | None, timeout: float, max_retries: int
) -> RequestSettings:
    """Settings for the email API; the key prefix is not checked."""
    key = environment.require(api_key, "EMAIL_API_KEY", hint=EMAIL_KEY_HINT)
    url = endpoint or environment.get("EMAIL_API_URL") or DEFAULT_EMAIL_URL
    return RequestSettings(
        base_url=url,
        headers={"Api-Key": key},
        timeout=timeout,
        max_retries=max_retries,
        error_factory=email_error_from_response,
    )


def resolve_service_id(environment: Environment, *candidates: str | None) -> str:
    """The first given service id, else RUNSITE_SERVICE_ID."""
    explicit = next((candidate for candidate in candidates if candidate), None)
    return environment.require(explicit, "RUNSITE_SERVICE_ID", hint=SERVICE_ID_HINT)


def resolve_bucket_name(environment: Environment, name: str | None) -> str:
    return environment.require(name, "S3_BUCKET", hint=BUCKET_HINT)


@dataclass(frozen=True)
class S3Credentials:
    key_id: str
    secret: str = field(repr=False)
    endpoint_url: str
    region: str


def s3_credentials(environment: Environment) -> S3Credentials:
    """Credentials from S3_*, or from AWS_* only when AWS_ENDPOINT_URL_S3 is set.

    AWS_* keys are never paired with S3_ENDPOINT_URL, so real AWS credentials are not sent to Runsite.
    """
    if environment.get("S3_ACCESS_KEY_ID") or not environment.get("AWS_ENDPOINT_URL_S3"):
        names = ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL")
    else:
        names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3")
    key_id, secret, endpoint = (environment.require(None, name, hint=S3_CREDENTIALS_HINT) for name in names)
    return S3Credentials(
        key_id=key_id,
        secret=secret,
        endpoint_url=endpoint,
        region=environment.get("S3_REGION") or DEFAULT_S3_REGION,
    )
