from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from runsite._errors import APIConnectionError, APIStatusError, APITimeoutError, ConfigurationError, parse_retry_after
from runsite._version import __version__

ErrorFactory = Callable[[httpx.Response], APIStatusError]

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


class SyncTransport:
    """Sends requests for one SDK client and applies the retry policy."""

    def __init__(self, settings: RequestSettings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._headers = settings.all_headers()
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client()

    def request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json: Any = None) -> Any:
        url = build_url(self._settings.base_url, path)
        query = without_none(params)
        max_retries = self._settings.max_retries
        for attempt in range(max_retries + 1):
            is_last_attempt = attempt == max_retries
            try:
                response = self._client.request(
                    method, url, params=query, json=json, headers=self._headers, timeout=self._settings.timeout
                )
            except httpx.TransportError as error:
                if is_last_attempt or not should_retry_error(method, error):
                    raise connection_error(method, error) from error
                _sleep(backoff_delay(attempt, None))
                continue
            except httpx.RequestError as error:
                raise request_failed_error(error) from error
            except httpx.InvalidURL as error:
                raise invalid_url_error(error) from error
            if is_last_attempt or not should_retry_status(method, response.status_code):
                return parse_response(response, self._settings.error_factory)
            _sleep(backoff_delay(attempt, response.headers.get("Retry-After")))
        raise AssertionError("retry loop ended without a result")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class AsyncTransport:
    """Async twin of SyncTransport with the same retry policy."""

    def __init__(self, settings: RequestSettings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._headers = settings.all_headers()
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient()

    async def request(
        self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json: Any = None
    ) -> Any:
        url = build_url(self._settings.base_url, path)
        query = without_none(params)
        max_retries = self._settings.max_retries
        for attempt in range(max_retries + 1):
            is_last_attempt = attempt == max_retries
            try:
                response = await self._client.request(
                    method, url, params=query, json=json, headers=self._headers, timeout=self._settings.timeout
                )
            except httpx.TransportError as error:
                if is_last_attempt or not should_retry_error(method, error):
                    raise connection_error(method, error) from error
                await _async_sleep(backoff_delay(attempt, None))
                continue
            except httpx.RequestError as error:
                raise request_failed_error(error) from error
            except httpx.InvalidURL as error:
                raise invalid_url_error(error) from error
            if is_last_attempt or not should_retry_status(method, response.status_code):
                return parse_response(response, self._settings.error_factory)
            await _async_sleep(backoff_delay(attempt, response.headers.get("Retry-After")))
        raise AssertionError("retry loop ended without a result")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
