from __future__ import annotations

import os

from runsite._errors import ConfigurationError

DEFAULT_BASE_URL = "https://api.runsite.app"
DEFAULT_EMAIL_URL = "https://api.runsite.app/emails"
API_PREFIX = "/api/v1"


def read_env(*names: str) -> str | None:
    """Return the first non-empty environment variable among `names`."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def resolve(explicit: str | None, *names: str, hint: str) -> str:
    """Return `explicit` or the first set variable, or raise naming the primary variable."""
    value = explicit or read_env(*names)
    if not value:
        raise ConfigurationError(f"{names[0]} is not set. {hint}".strip())
    return value
