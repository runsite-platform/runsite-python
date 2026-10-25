"""The two HTTP APIs the SDK talks to: where each lives, how it authenticates and how it reports errors.

`platform_api` and `email_api` check `timeout` and `max_retries` at once and return a settings provider.
The provider reads the key and URL from the environment only when the client sends its first request,
so a missing setting is reported then, after the caller's own input was validated.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import httpx

from runsite._environment import Environment
from runsite._errors import (
    STATUS_ERROR_CLASSES,
    APIStatusError,
    ConfigurationError,
    InternalServerError,
    validation_errors,
)
from runsite._http import RequestSettings, SettingsProvider, check_limits

DEFAULT_BASE_URL = "https://api.runsite.app"
DEFAULT_EMAIL_URL = "https://api.runsite.app/emails"
API_PREFIX = "/api/v1"
API_KEY_PREFIX = "ak_live_"
API_KEY_HINT = "Create an API key in the Runsite dashboard (Settings → API keys) and set RUNSITE_API_KEY."
EMAIL_KEY_HINT = "Connect an email service to this web service in the Runsite dashboard, or pass api_key=..."
MAX_TEXT_DETAIL_LENGTH = 500


def platform_api(
    environment: Environment, *, api_key: str | None, base_url: str | None, timeout: float, max_retries: int
) -> SettingsProvider:
    """Settings for the Runsite platform API (/api/v1), resolved on first use."""
    check_limits(timeout, max_retries)
    return partial(
        _platform_settings, environment, api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
    )


def email_api(
    environment: Environment, *, api_key: str | None, endpoint: str | None, timeout: float, max_retries: int
) -> SettingsProvider:
    """Settings for the email API, resolved on first use; the key prefix is not checked."""
    check_limits(timeout, max_retries)
    return partial(
        _email_settings, environment, api_key=api_key, endpoint=endpoint, timeout=timeout, max_retries=max_retries
    )


def _platform_settings(
    environment: Environment, *, api_key: str | None, base_url: str | None, timeout: float, max_retries: int
) -> RequestSettings:
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


def _email_settings(
    environment: Environment, *, api_key: str | None, endpoint: str | None, timeout: float, max_retries: int
) -> RequestSettings:
    key = environment.require(api_key, "EMAIL_API_KEY", hint=EMAIL_KEY_HINT)
    url = endpoint or environment.get("EMAIL_API_URL") or DEFAULT_EMAIL_URL
    return RequestSettings(
        base_url=url,
        headers={"Api-Key": key},
        timeout=timeout,
        max_retries=max_retries,
        error_factory=email_error_from_response,
    )


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
