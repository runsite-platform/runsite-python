from __future__ import annotations

import builtins
import re
from collections.abc import Iterable, Mapping
from functools import partial
from typing import Any

import httpx

from runsite._client import AsyncClient, SyncClient
from runsite._config import Environment, platform_settings, resolve_service_id
from runsite._errors import ConflictError
from runsite._http import Operation, Request
from runsite._secrets.models import ApplyResult, BulkResult, EnvVariable
from runsite._secrets.reader import SecretReader

KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")
MAX_KEY_LENGTH = 255
RESERVED_KEYS = frozenset({"APP_PORT", "APP_NAME", "RUNSITE_SERVICE_ID"})
VALID_PORTS = range(1, 65536)


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


def validate_variables(values: Mapping[str, object]) -> None:
    for key, value in values.items():
        validate_variable(key, value)


def delete_body(keys: Iterable[str], existing: Mapping[str, EnvVariable]) -> dict[str, Any]:
    return {"variables": [{"id": existing[key].id, "key": key, "deleted": True} for key in keys if key in existing]}


class SecretsOperations:
    """The writer operations of `Secrets` and `AsyncSecrets`, for one environment and default service."""

    def __init__(self, environment: Environment, *, service_id: str | None) -> None:
        self._environment = environment
        self._service_id = service_id

    def _target(self, call_service_id: str | None) -> str:
        return resolve_service_id(self._environment, call_service_id, self._service_id)

    def list(self, service_id: str | None) -> Operation[builtins.list[EnvVariable]]:
        return parse_variables((yield Request("GET", env_path(self._target(service_id)))))

    def set_many(self, values: Mapping[str, str], secret: bool | None, service_id: str | None) -> Operation[BulkResult]:
        validate_variables(values)
        if not values:
            return BulkResult()
        target = self._target(service_id)
        try:
            return (yield from _upsert(target, values, secret))
        except ConflictError:
            return (yield from _upsert(target, values, secret))

    def delete(self, keys: tuple[str, ...], service_id: str | None) -> Operation[BulkResult]:
        target = self._target(service_id)
        body = delete_body(keys, variables_by_key((yield Request("GET", env_path(target)))))
        if not body["variables"]:
            return BulkResult()
        return BulkResult.model_validate((yield Request("PUT", bulk_path(target), json=body)))

    def apply(self, service_id: str | None) -> Operation[ApplyResult]:
        return ApplyResult.model_validate((yield Request("POST", apply_path(self._target(service_id)))))


def _upsert(target: str, values: Mapping[str, str], secret: bool | None) -> Operation[BulkResult]:
    existing = variables_by_key((yield Request("GET", env_path(target))))
    response = yield Request("PUT", bulk_path(target), json=upsert_body(values, existing, secret))
    return BulkResult.model_validate(response)


class Secrets(SecretReader, SyncClient):
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
        SecretReader.__init__(self, environ=environ)
        settings = partial(
            platform_settings,
            self._environment,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        SyncClient.__init__(self, settings, http_client=http_client)
        self._operations = SecretsOperations(self._environment, service_id=service_id)

    def list(self, *, service_id: str | None = None) -> builtins.list[EnvVariable]:
        """List the service's variables: keys and flags only, values are never returned."""
        return self._run(self._operations.list(service_id))

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
        return self._run(self._operations.set_many(values, secret, service_id))

    def delete(self, *keys: str, service_id: str | None = None) -> BulkResult:
        """Delete variables by key; keys that do not exist are ignored."""
        return self._run(self._operations.delete(keys, service_id))

    def apply(self, *, service_id: str | None = None) -> ApplyResult:
        """Push the stored variables to the service with a rolling restart (no rebuild).

        Returns as soon as the restart is queued (`result.restarting` is True); poll
        `GET /web-services/{id}` to follow it. A service that is not running returns its current status
        instead: the variables are saved and apply on its next start.

        Raises `ConflictError` (409) when the service is being deleted or another start, stop, restart or
        deploy is in progress, `BadRequestError` (400) when the service is blocked or out of traffic, and an
        `InternalServerError` subclass (503) when the restart could not be queued; retry it later.
        """
        return self._run(self._operations.apply(service_id))


class AsyncSecrets(SecretReader, AsyncClient):
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
        SecretReader.__init__(self, environ=environ)
        settings = partial(
            platform_settings,
            self._environment,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        AsyncClient.__init__(self, settings, http_client=http_client)
        self._operations = SecretsOperations(self._environment, service_id=service_id)

    async def list(self, *, service_id: str | None = None) -> builtins.list[EnvVariable]:
        """See `Secrets.list`."""
        return await self._run(self._operations.list(service_id))

    async def set(
        self, key: str, value: str, *, secret: bool | None = None, service_id: str | None = None
    ) -> BulkResult:
        """See `Secrets.set`."""
        return await self.set_many({key: value}, secret=secret, service_id=service_id)

    async def set_many(
        self, values: Mapping[str, str], *, secret: bool | None = None, service_id: str | None = None
    ) -> BulkResult:
        """See `Secrets.set_many`."""
        return await self._run(self._operations.set_many(values, secret, service_id))

    async def delete(self, *keys: str, service_id: str | None = None) -> BulkResult:
        """See `Secrets.delete`."""
        return await self._run(self._operations.delete(keys, service_id))

    async def apply(self, *, service_id: str | None = None) -> ApplyResult:
        """See `Secrets.apply`."""
        return await self._run(self._operations.apply(service_id))
