from __future__ import annotations

import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

MAX_TEXT_DETAIL_LENGTH = 500


class RunsiteError(Exception):
    """Base class for every error raised by the SDK."""


class ConfigurationError(RunsiteError):
    """A required setting is missing or invalid."""


class MissingSecretError(RunsiteError):
    """One or more environment variables are not set."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        label = "variable" if len(keys) == 1 else "variables"
        super().__init__(f"Missing environment {label}: {', '.join(keys)}")


class InvalidSecretError(RunsiteError):
    """An environment variable is set but cannot be converted to the requested type."""


class WebhookVerificationError(RunsiteError):
    """A webhook signature or timestamp did not verify."""


class StorageError(RunsiteError):
    """An object storage call failed."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class ObjectNotFoundError(StorageError):
    """The bucket or object does not exist."""


class StorageAccessDeniedError(StorageError):
    """The storage credentials do not allow this operation."""


class APIConnectionError(RunsiteError):
    """The request did not get a usable response."""


class APITimeoutError(APIConnectionError):
    """The request was sent but no response arrived in time."""


def _seconds_until_http_date(header: str, now: datetime | None) -> float | None:
    try:
        moment = parsedate_to_datetime(header)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - (now or datetime.now(timezone.utc))).total_seconds()


def parse_retry_after(header: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds from a Retry-After header (delay-seconds or HTTP date); None when missing or unusable.

    The result can be negative for a date in the past; callers clamp it.
    """
    if header is None:
        return None
    try:
        seconds: float | None = float(header)
    except ValueError:
        seconds = _seconds_until_http_date(header, now)
    if seconds is None or not math.isfinite(seconds):
        return None
    return seconds


def _detail_text(detail: Any, *names: str) -> str | None:
    """First non-empty field among `names` of a dict detail, as a string."""
    if not isinstance(detail, dict):
        return None
    for name in names:
        value = detail.get(name)
        if value is not None and value != "":
            return str(value)
    return None


class APIStatusError(RunsiteError):
    """A Runsite API answered with an error status code."""

    def __init__(self, message: str, *, response: httpx.Response, detail: Any) -> None:
        super().__init__(message)
        self.response = response
        self.status_code = response.status_code
        self.detail = detail

    @property
    def code(self) -> str | None:
        """Machine-readable error code, when the API sent one."""
        return _detail_text(self.detail, "code", "type")

    @property
    def retry_after(self) -> float | None:
        """Seconds the API asked to wait (0 or more), if it said."""
        seconds = parse_retry_after(self.response.headers.get("Retry-After"))
        return max(seconds, 0.0) if seconds is not None else None


class BadRequestError(APIStatusError):
    """400: the request was rejected."""


class AuthenticationError(APIStatusError):
    """401: the API key is missing, invalid, revoked or expired."""


class EmailQuotaExceededError(APIStatusError):
    """402: the email quota is used up and no more can be bought from the balance."""

    @property
    def quota(self) -> Any:
        return self.detail.get("quota") if isinstance(self.detail, dict) else None


class PermissionDeniedError(APIStatusError):
    """403: the key scope or the owner's role does not allow this action."""

    @property
    def required_scope(self) -> str | None:
        return _detail_text(self.detail, "required")

    @property
    def actual_scope(self) -> str | None:
        return _detail_text(self.detail, "actual")


class NotFoundError(APIStatusError):
    """404: the resource does not exist or is not visible to this key."""


class ConflictError(APIStatusError):
    """409: the resource already exists or another action on it is running."""


class UnprocessableEntityError(APIStatusError):
    """422: the request failed validation."""

    @property
    def errors(self) -> list[tuple[str, str]]:
        """Validation errors as (field, message) pairs."""
        return validation_errors(self.detail) if isinstance(self.detail, list) else []


class RateLimitError(APIStatusError):
    """429: too many requests."""


class InternalServerError(APIStatusError):
    """5xx: the service failed to handle the request."""


class UpstreamError(InternalServerError):
    """502: a service behind Runsite (for example the email provider or object storage) failed."""


class ServiceUnavailableError(InternalServerError):
    """503: the service is temporarily unavailable; retry after `retry_after` seconds."""


STATUS_ERROR_CLASSES: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    402: EmailQuotaExceededError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
    502: UpstreamError,
    503: ServiceUnavailableError,
}
LOCATION_PREFIXES = ("body", "query", "path")


def validation_errors(detail: list[Any]) -> list[tuple[str, str]]:
    pairs = []
    for item in detail:
        if not isinstance(item, dict):
            pairs.append(("", str(item)))
            continue
        location = ".".join(str(part) for part in item.get("loc", []) if part not in LOCATION_PREFIXES)
        pairs.append((location, str(item.get("msg", "invalid value"))))
    return pairs


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _text_body(response: httpx.Response) -> str | None:
    """Plain-text error body (for example an axum rejection), shortened; None when empty."""
    return response.text.strip()[:MAX_TEXT_DETAIL_LENGTH] or None


def _status_class(status_code: int) -> type[APIStatusError]:
    error_class = STATUS_ERROR_CLASSES.get(status_code)
    if error_class is not None:
        return error_class
    return InternalServerError if status_code >= 500 else APIStatusError


def _describe_api_detail(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        return "; ".join(f"{field}: {message}" if field else message for field, message in validation_errors(detail))
    if isinstance(detail, dict):
        if detail.get("code") == "insufficient_scope":
            return f"API key scope '{detail.get('actual')}' is below the required '{detail.get('required')}'"
        if detail.get("code"):
            reason = detail.get("reason") or detail.get("message")
            return f"{detail['code']}: {reason}" if reason else str(detail["code"])
    return ""


def api_error_from_response(response: httpx.Response) -> APIStatusError:
    """Build the error for a /api/v1 error response ({"detail": ...})."""
    body = _json_body(response)
    if isinstance(body, dict):
        detail = body.get("detail")
    elif body is None:
        detail = _text_body(response)
    else:
        detail = None
    description = _describe_api_detail(detail) or response.reason_phrase
    error_class = _status_class(response.status_code)
    return error_class(f"{response.status_code} {description}", response=response, detail=detail)


def email_error_from_response(response: httpx.Response) -> APIStatusError:
    """Build the error for an email API response ({"error": {...}} or {"error": "..."})."""
    body = _json_body(response)
    if isinstance(body, dict):
        error = body.get("error")
    elif body is None:
        error = _text_body(response)
    else:
        error = None
    message: Any = None
    if isinstance(error, dict):
        message = error.get("message")
    elif isinstance(error, str):
        message = error
    status_code = response.status_code
    error_class = _status_class(status_code)
    return error_class(f"{status_code} {message or response.reason_phrase}", response=response, detail=error)
