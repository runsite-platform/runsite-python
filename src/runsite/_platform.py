from __future__ import annotations

from runsite._config import API_PREFIX, DEFAULT_BASE_URL, read_env, resolve
from runsite._errors import ConfigurationError, api_error_from_response
from runsite._http import RequestSettings

API_KEY_HINT = "Create an API key in the Runsite dashboard (Settings → API keys) and set RUNSITE_API_KEY."
API_KEY_PREFIX = "ak_live_"


def platform_settings(
    *, api_key: str | None, base_url: str | None, timeout: float, max_retries: int
) -> RequestSettings:
    """Settings for calls to the Runsite platform API (/api/v1)."""
    key = resolve(api_key, "RUNSITE_API_KEY", hint=API_KEY_HINT)
    if not key.startswith(API_KEY_PREFIX):
        raise ConfigurationError(f"RUNSITE_API_KEY must be a platform API key starting with '{API_KEY_PREFIX}'.")
    root = base_url or read_env("RUNSITE_BASE_URL") or DEFAULT_BASE_URL
    return RequestSettings(
        base_url=f"{root.rstrip('/')}{API_PREFIX}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
        max_retries=max_retries,
        error_factory=api_error_from_response,
    )
