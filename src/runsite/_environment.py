"""How the SDK reads environment variables.

Each client takes an optional `environ=` mapping and wraps it in an `Environment`; without one it reads
`os.environ` at the moment a setting is needed. Explicit constructor arguments always win. Each setting is
resolved next to the code that uses it: the HTTP APIs in `_dialects`, the service id in `_secrets.writer`,
the bucket and S3 credentials in `_storage.registry`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from runsite._errors import ConfigurationError


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
