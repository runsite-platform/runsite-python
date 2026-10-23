from __future__ import annotations

import builtins
import re
from collections.abc import Iterable, Mapping
from types import TracebackType
from typing import Any

import httpx

from runsite._config import DEFAULT_BASE_URL
from runsite._errors import ConfigurationError, ConflictError
from runsite._http import AsyncTransport, RequestSettings, SyncTransport
from runsite._platform import API_KEY_HINT, platform_settings
from runsite._secrets.models import ApplyResult, BulkResult, EnvVariable
from runsite._secrets.reader import SecretReader

KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")
MAX_KEY_LENGTH = 255
RESERVED_KEYS = frozenset({"APP_PORT", "APP_NAME", "RUNSITE_SERVICE_ID"})
VALID_PORTS = range(1, 65536)
SERVICE_ID_HINT = "It is set automatically inside Runsite containers; elsewhere pass service_id=..."


def _is_valid_port(value: str) -> bool:
    try:
        return int(value) in VALID_PORTS
    except ValueError:
        return False


def validate_variable(key: str, value: object) -> None:
    """Reject what the API would reject, before any request; messages never contain the value."""
    if len(key) > MAX_KEY_LENGTH or not KEY_PATTERN.fullmatch(key):
        raise ValueError(
            f"{key!r} is not a valid key: use up to {MAX_KEY_LENGTH} uppercase letters, digits and underscores, "
            "starting with a letter"
        )
    if key in RESERVED_KEYS:
        raise ValueError(f"{key} is managed by the platform and cannot be set")
    if not isinstance(value, str):
        raise TypeError(f"{key}: value must be a str, got {type(value).__name__}")
    if value == "":
        raise ValueError(f"{key}: value must not be empty")
    if key == "PORT" and not _is_valid_port(value):
        raise ValueError("PORT must be an integer between 1 and 65535")


def env_path(service_id: str) -> str:
    return f"/web-services/{service_id}/env"


def bulk_path(service_id: str) -> str:
    return f"{env_path(service_id)}/bulk"


def apply_path(service_id: str) -> str:
    return f"/web-services/{service_id}/apply-env"


def parse_variables(listing: Mapping[str, Any]) -> list[EnvVariable]:
    return [EnvVariable.model_validate(item) for item in listing["environment_variables"]]


def variables_by_key(listing: Mapping[str, Any]) -> dict[str, EnvVariable]:
    return {variable.key: variable for variable in parse_variables(listing)}


def upsert_body(values: Mapping[str, str], existing: Mapping[str, EnvVariable], secret: bool | None) -> dict[str, Any]:
    """Bulk items: updates keep the stored `is_secret` unless `secret` is given; creates default to secret."""
    items: list[dict[str, Any]] = []
    for key, value in values.items():
        current = existing.get(key)
        if current is None:
            items.append({"key": key, "value": value, "is_secret": True if secret is None else secret})
        else:
            is_secret = current.is_secret if secret is None else secret
            items.append({"id": current.id, "key": key, "value": value, "is_secret": is_secret})
    return {"variables": items}


def delete_body(keys: Iterable[str], existing: Mapping[str, EnvVariable]) -> dict[str, Any]:
    return {"variables": [{"id": existing[key].id, "key": key, "deleted": True} for key in keys if key in existing]}


class _WriterConfig:
    def __init__(
        self, *, api_key: str | None, base_url: str | None, service_id: str | None, timeout: float, max_retries: int
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.service_id = service_id
        self.timeout = timeout
        self.max_retries = max_retries

    def target(self, service_id: str | None, source: Mapping[str, str]) -> str:
        resolved = service_id or self.service_id or source.get("RUNSITE_SERVICE_ID")
        if not resolved:
            raise ConfigurationError(f"RUNSITE_SERVICE_ID is not set. {SERVICE_ID_HINT}")
        return resolved

    def settings(self, source: Mapping[str, str]) -> RequestSettings:
        api_key = self.api_key or source.get("RUNSITE_API_KEY")
        if not api_key:
            raise ConfigurationError(f"RUNSITE_API_KEY is not set. {API_KEY_HINT}")
        base_url = self.base_url or source.get("RUNSITE_BASE_URL") or DEFAULT_BASE_URL
        return platform_settings(api_key=api_key, base_url=base_url, timeout=self.timeout, max_retries=self.max_retries)


class Secrets(SecretReader):
    """Reads variables from the environment and writes them to a Runsite web service.

    The service id, API key and base URL come from the constructor arguments, else from the same mapping
    the reader uses (`environ=`, or `os.environ`): `RUNSITE_SERVICE_ID`, `RUNSITE_API_KEY`, `RUNSITE_BASE_URL`.

    `list()` needs an API key with the `read` scope. `set`, `set_many`, `delete` and `apply` need the `write`
    scope, and the key's owner must be an ADMIN of the workspace; otherwise `PermissionDeniedError` is raised.
    Writes are rate limited to 30 per minute (`RateLimitError`); listing is not. An unknown service id, or one
    from another workspace, raises `NotFoundError`.

    Writes are stored at once but reach the running container only after `apply()` or the next deploy.
    Setting a key that a connection injects (for example `DATABASE_URL`) overrides the injected value.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        service_id: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        http_client: httpx.Client | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(environ=environ)
        self._config = _WriterConfig(
            api_key=api_key, base_url=base_url, service_id=service_id, timeout=timeout, max_retries=max_retries
        )
        self._http_client = http_client
        self._transport: SyncTransport | None = None

    def _client(self) -> SyncTransport:
        if self._transport is None:
            self._transport = SyncTransport(self._config.settings(self._source()), http_client=self._http_client)
        return self._transport

    def _target(self, service_id: str | None) -> str:
        return self._config.target(service_id, self._source())

    def list(self, *, service_id: str | None = None) -> builtins.list[EnvVariable]:
        """List the service's variables: keys and flags only, values are never returned."""
        return parse_variables(self._client().request("GET", env_path(self._target(service_id))))

    def set(self, key: str, value: str, *, secret: bool | None = None, service_id: str | None = None) -> BulkResult:
        """Create or update one variable; see `set_many` for the `secret` flag."""
        return self.set_many({key: value}, secret=secret, service_id=service_id)

    def set_many(
        self, values: Mapping[str, str], *, secret: bool | None = None, service_id: str | None = None
    ) -> BulkResult:
        """Create or update several variables in one request; variables not listed stay unchanged.

        `secret=None` keeps the stored flag of an existing variable and marks a new one as secret; True or
        False sets the flag on every listed variable. Keys and values are validated before any request
        (`ValueError`, or `TypeError` for a non-str value). If another writer creates one of the keys at the
        same moment, the read-and-write is repeated once; a second conflict raises `ConflictError`.
        """
        for key, value in values.items():
            validate_variable(key, value)
        if not values:
            return BulkResult()
        target = self._target(service_id)
        try:
            return self._upsert(target, values, secret)
        except ConflictError:
            return self._upsert(target, values, secret)

    def _upsert(self, target: str, values: Mapping[str, str], secret: bool | None) -> BulkResult:
        client = self._client()
        existing = variables_by_key(client.request("GET", env_path(target)))
        response = client.request("PUT", bulk_path(target), json=upsert_body(values, existing, secret))
        return BulkResult.model_validate(response)

    def delete(self, *keys: str, service_id: str | None = None) -> BulkResult:
        """Delete variables by key; keys that do not exist are ignored."""
        target = self._target(service_id)
        client = self._client()
        body = delete_body(keys, variables_by_key(client.request("GET", env_path(target))))
        if not body["variables"]:
            return BulkResult()
        return BulkResult.model_validate(client.request("PUT", bulk_path(target), json=body))

    def apply(self, *, service_id: str | None = None) -> ApplyResult:
        """Push the stored variables to the service with a rolling restart (no rebuild).

        Returns as soon as the restart is queued (`result.restarting` is True); poll
        `GET /web-services/{id}` to follow it. A service that is not running returns its current status
        instead: the variables are saved and apply on its next start.

        Raises `ConflictError` (409) when the service is being deleted or another start, stop, restart or
        deploy is in progress, `BadRequestError` (400) when the service is blocked or out of traffic, and an
        `InternalServerError` subclass (503) when the restart could not be queued; retry it later.
        """
        return ApplyResult.model_validate(self._client().request("POST", apply_path(self._target(service_id))))

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def __enter__(self) -> Secrets:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()


class AsyncSecrets(SecretReader):
    """Async twin of `Secrets` with the same configuration, permissions and semantics; reads stay synchronous."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        service_id: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        http_client: httpx.AsyncClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(environ=environ)
        self._config = _WriterConfig(
            api_key=api_key, base_url=base_url, service_id=service_id, timeout=timeout, max_retries=max_retries
        )
        self._http_client = http_client
        self._transport: AsyncTransport | None = None

    def _client(self) -> AsyncTransport:
        if self._transport is None:
            self._transport = AsyncTransport(self._config.settings(self._source()), http_client=self._http_client)
        return self._transport

    def _target(self, service_id: str | None) -> str:
        return self._config.target(service_id, self._source())

    async def list(self, *, service_id: str | None = None) -> builtins.list[EnvVariable]:
        """See `Secrets.list`."""
        return parse_variables(await self._client().request("GET", env_path(self._target(service_id))))

    async def set(
        self, key: str, value: str, *, secret: bool | None = None, service_id: str | None = None
    ) -> BulkResult:
        """See `Secrets.set`."""
        return await self.set_many({key: value}, secret=secret, service_id=service_id)

    async def set_many(
        self, values: Mapping[str, str], *, secret: bool | None = None, service_id: str | None = None
    ) -> BulkResult:
        """See `Secrets.set_many`."""
        for key, value in values.items():
            validate_variable(key, value)
        if not values:
            return BulkResult()
        target = self._target(service_id)
        try:
            return await self._upsert(target, values, secret)
        except ConflictError:
            return await self._upsert(target, values, secret)

    async def _upsert(self, target: str, values: Mapping[str, str], secret: bool | None) -> BulkResult:
        client = self._client()
        existing = variables_by_key(await client.request("GET", env_path(target)))
        response = await client.request("PUT", bulk_path(target), json=upsert_body(values, existing, secret))
        return BulkResult.model_validate(response)

    async def delete(self, *keys: str, service_id: str | None = None) -> BulkResult:
        """See `Secrets.delete`."""
        target = self._target(service_id)
        client = self._client()
        body = delete_body(keys, variables_by_key(await client.request("GET", env_path(target))))
        if not body["variables"]:
            return BulkResult()
        return BulkResult.model_validate(await client.request("PUT", bulk_path(target), json=body))

    async def apply(self, *, service_id: str | None = None) -> ApplyResult:
        """See `Secrets.apply`."""
        response = await self._client().request("POST", apply_path(self._target(service_id)))
        return ApplyResult.model_validate(response)

    async def aclose(self) -> None:
        if self._transport is not None:
            await self._transport.aclose()
            self._transport = None

    async def __aenter__(self) -> AsyncSecrets:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()
