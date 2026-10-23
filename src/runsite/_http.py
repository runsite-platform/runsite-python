"""HTTP plumbing shared by every SDK client.

SDK methods are written once, as operations: generators that yield `Request`s, receive the parsed JSON
body of each response, and return the method's result. `execute` wraps an operation with the retry policy
and turns it into a stream of `Call` and `Wait` steps. Only the two small drivers, `drive_sync` and
`drive_async`, touch the network or the clock, so the sync and async clients share all behaviour.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx

from runsite._errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    ConfigurationError,
    RunsiteError,
    parse_retry_after,
)
from runsite._version import __version__

ResultT = TypeVar("ResultT")
ErrorFactory = Callable[[httpx.Response], APIStatusError]
SettingsProvider = Callable[[], "RequestSettings"]

RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
SAFE_TO_REPEAT_METHODS = frozenset({"GET", "HEAD"})
RETRYABLE_TRANSPORT_ERRORS = (httpx.NetworkError, httpx.TimeoutException)
NOT_SENT_CLIENT_ERRORS = (httpx.UnsupportedProtocol, httpx.LocalProtocolError, httpx.ProxyError)
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0
RETRY_AFTER_CAP_SECONDS = 60.0
UNREACHABLE_MESSAGE = "Could not reach the Runsite service."
NO_READ_RESPONSE_MESSAGE = "The Runsite service did not respond."
NO_WRITE_RESPONSE_MESSAGE = (
    "The Runsite service did not respond, so the action may or may not have been applied. "
    "Check the current state before retrying."
)
INVALID_BODY_MESSAGE = "The Runsite service returned a response that is not valid JSON."


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


async def _async_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass(frozen=True)
class RequestSettings:
    """Where and how one SDK client sends its requests."""

    base_url: str
    headers: Mapping[str, str]
    timeout: float
    max_retries: int
    error_factory: ErrorFactory = field(repr=False)

    def __post_init__(self) -> None:
        if not (isinstance(self.timeout, (int, float)) and math.isfinite(self.timeout) and self.timeout > 0):
            raise ValueError(f"timeout must be a positive number of seconds, got {self.timeout!r}")
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be 0 or more, got {self.max_retries}")

    def all_headers(self) -> dict[str, str]:
        return {"User-Agent": f"runsite-python/{__version__}", "Accept": "application/json", **self.headers}


@dataclass(frozen=True)
class Request:
    """One API call an operation asks for. `min_timeout` raises the client timeout for slow endpoints."""

    method: str
    path: str
    params: Mapping[str, Any] | None = None
    json: Any = None
    min_timeout: float | None = None


@dataclass(frozen=True)
class Call:
    """Send this HTTP request; the driver answers with the `httpx.Response` or throws the httpx error."""

    method: str
    url: str
    params: dict[str, Any] | None
    json: Any
    headers: dict[str, str]
    timeout: float


@dataclass(frozen=True)
class Wait:
    """Sleep this many seconds before the next step."""

    seconds: float


Operation = Generator[Request, Any, ResultT]
Steps = Generator["Call | Wait", Any, ResultT]


def build_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def without_none(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {name: value for name, value in params.items() if value is not None}


def backoff_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before retry number `attempt + 1`."""
    requested = parse_retry_after(retry_after)
    if requested is not None:
        return min(max(requested, 0.0), RETRY_AFTER_CAP_SECONDS)
    return min(BACKOFF_BASE_SECONDS * 2.0**attempt, BACKOFF_CAP_SECONDS)


def never_sent(error: httpx.TransportError) -> bool:
    """These failures happen before any byte of the request leaves the client."""
    return isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


def should_retry_error(method: str, error: httpx.TransportError) -> bool:
    if not isinstance(error, RETRYABLE_TRANSPORT_ERRORS):
        return False
    return never_sent(error) or method in SAFE_TO_REPEAT_METHODS


def should_retry_status(method: str, status_code: int) -> bool:
    return method in SAFE_TO_REPEAT_METHODS and status_code in RETRYABLE_STATUS_CODES


def connection_error(method: str, error: httpx.TransportError) -> APIConnectionError:
    if never_sent(error):
        if isinstance(error, httpx.TimeoutException):
            return APITimeoutError(UNREACHABLE_MESSAGE)
        return APIConnectionError(UNREACHABLE_MESSAGE)
    if isinstance(error, NOT_SENT_CLIENT_ERRORS):
        return request_failed_error(error)
    message = NO_READ_RESPONSE_MESSAGE if method in SAFE_TO_REPEAT_METHODS else NO_WRITE_RESPONSE_MESSAGE
    if isinstance(error, httpx.TimeoutException):
        return APITimeoutError(message)
    return APIConnectionError(message)


def request_failed_error(error: httpx.RequestError) -> APIConnectionError:
    return APIConnectionError(f"Request to the Runsite service failed: {error}")


def invalid_url_error(error: httpx.InvalidURL) -> ConfigurationError:
    return ConfigurationError(f"Invalid Runsite service URL: {error}")


def parse_response(response: httpx.Response, error_factory: ErrorFactory) -> Any:
    """Return the JSON body of a 2xx response; anything else (including 3xx) raises."""
    if not response.is_success:
        raise error_factory(response)
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as error:
        raise APIConnectionError(INVALID_BODY_MESSAGE) from error


def exchange(settings: RequestSettings, request: Request) -> Steps[Any]:
    """Send one request with the retry policy and return its parsed JSON body."""
    timeout = max(settings.timeout, request.min_timeout or 0.0)
    call = Call(
        method=request.method,
        url=build_url(settings.base_url, request.path),
        params=without_none(request.params),
        json=request.json,
        headers=settings.all_headers(),
        timeout=timeout,
    )
    max_retries = settings.max_retries
    for attempt in range(max_retries + 1):
        is_last_attempt = attempt == max_retries
        try:
            response = yield call
        except httpx.TransportError as error:
            if is_last_attempt or not should_retry_error(request.method, error):
                raise connection_error(request.method, error) from error
            yield Wait(backoff_delay(attempt, None))
            continue
        except httpx.RequestError as error:
            raise request_failed_error(error) from error
        except httpx.InvalidURL as error:
            raise invalid_url_error(error) from error
        if is_last_attempt or not should_retry_status(request.method, response.status_code):
            return parse_response(response, settings.error_factory)
        yield Wait(backoff_delay(attempt, response.headers.get("Retry-After")))
    raise AssertionError("retry loop ended without a result")


def execute(settings: SettingsProvider, operation: Operation[ResultT]) -> Steps[ResultT]:
    """Run an operation, sending each request it yields; SDK errors are thrown back into it.

    Settings are resolved at the first request, so checks inside the operation run before configuration
    errors. The loop ends when the operation returns or raises.
    """
    outcome: Any = None
    failure: RunsiteError | None = None
    while True:
        try:
            request = operation.send(outcome) if failure is None else operation.throw(failure)
        except StopIteration as stop:
            result: ResultT = stop.value
            return result
        failure = None
        try:
            outcome = yield from exchange(settings(), request)
        except RunsiteError as error:
            failure = error


def single(request: Request) -> Operation[Any]:
    """An operation that sends one request and returns its parsed body."""
    return (yield request)


HTTP_ERRORS = (httpx.HTTPError, httpx.InvalidURL)


def drive_sync(steps: Steps[ResultT], client: httpx.Client) -> ResultT:
    reply: Any = None
    failure: Exception | None = None
    while True:
        try:
            step = steps.send(reply) if failure is None else steps.throw(failure)
        except StopIteration as stop:
            result: ResultT = stop.value
            return result
        reply, failure = None, None
        if isinstance(step, Wait):
            _sleep(step.seconds)
            continue
        try:
            reply = client.request(
                step.method, step.url, params=step.params, json=step.json, headers=step.headers, timeout=step.timeout
            )
        except HTTP_ERRORS as error:
            failure = error


async def drive_async(steps: Steps[ResultT], client: httpx.AsyncClient) -> ResultT:
    reply: Any = None
    failure: Exception | None = None
    while True:
        try:
            step = steps.send(reply) if failure is None else steps.throw(failure)
        except StopIteration as stop:
            result: ResultT = stop.value
            return result
        reply, failure = None, None
        if isinstance(step, Wait):
            await _async_sleep(step.seconds)
            continue
        try:
            reply = await client.request(
                step.method, step.url, params=step.params, json=step.json, headers=step.headers, timeout=step.timeout
            )
        except HTTP_ERRORS as error:
            failure = error


class _CachedSettings:
    def __init__(self, provider: SettingsProvider) -> None:
        self._provider = provider
        self._settings: RequestSettings | None = None

    def __call__(self) -> RequestSettings:
        if self._settings is None:
            self._settings = self._provider()
        return self._settings

    def reset(self) -> None:
        self._settings = None


class SyncTransport:
    """Runs operations for one SDK client over a lazily opened connection pool.

    Settings are resolved on the first request and again after `close()`, which also drops the pool;
    the transport stays usable. A supplied `http_client` is used as is and never closed.
    """

    def __init__(self, settings: SettingsProvider, *, http_client: httpx.Client | None = None) -> None:
        self._settings = _CachedSettings(settings)
        self._supplied_client = http_client
        self._owned_client: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        if self._supplied_client is not None:
            return self._supplied_client
        if self._owned_client is None:
            self._owned_client = httpx.Client()
        return self._owned_client

    def run(self, operation: Operation[ResultT]) -> ResultT:
        return drive_sync(execute(self._settings, operation), self._client())

    def request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json: Any = None) -> Any:
        return self.run(single(Request(method, path, params=params, json=json)))

    def close(self) -> None:
        if self._owned_client is not None:
            self._owned_client.close()
            self._owned_client = None
        self._settings.reset()


class AsyncTransport:
    """Async twin of `SyncTransport`."""

    def __init__(self, settings: SettingsProvider, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = _CachedSettings(settings)
        self._supplied_client = http_client
        self._owned_client: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._supplied_client is not None:
            return self._supplied_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient()
        return self._owned_client

    async def run(self, operation: Operation[ResultT]) -> ResultT:
        return await drive_async(execute(self._settings, operation), self._client())

    async def request(
        self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json: Any = None
    ) -> Any:
        return await self.run(single(Request(method, path, params=params, json=json)))

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None
        self._settings.reset()
