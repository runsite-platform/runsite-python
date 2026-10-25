from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, overload

from runsite._environment import Environment
from runsite._errors import InvalidSecretError, MissingSecretError
from runsite._secret import Secret

DefaultT = TypeVar("DefaultT")
ValueT = TypeVar("ValueT")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class _Missing:
    pass


MISSING: Any = _Missing()


def _to_bool(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError("not a boolean")


class SecretReader:
    """Reads configuration from environment variables, converting types on the way.

    A variable set to an empty string counts as unset everywhere: getters return the default or raise
    `MissingSecretError`. A non-empty value that cannot be converted raises `InvalidSecretError`, whose
    message names the key but never contains the value.
    """

    def __init__(self, *, environ: Mapping[str, str] | None = None) -> None:
        self._environment = Environment(environ)

    def _convert(self, key: str, default: Any, convert: Callable[[str], ValueT], type_name: str) -> Any:
        raw = self._environment.get(key)
        if raw is None:
            if default is MISSING:
                raise MissingSecretError([key])
            return default
        try:
            return convert(raw)
        except (ValueError, TypeError):
            raise InvalidSecretError(f"{key} is set but is not a valid {type_name}") from None

    @overload
    def get(self, key: str) -> str: ...
    @overload
    def get(self, key: str, default: DefaultT) -> str | DefaultT: ...
    def get(self, key: str, default: Any = MISSING) -> Any:
        """Return the variable as a string."""
        return self._convert(key, default, str, "string")

    @overload
    def get_int(self, key: str) -> int: ...
    @overload
    def get_int(self, key: str, default: DefaultT) -> int | DefaultT: ...
    def get_int(self, key: str, default: Any = MISSING) -> Any:
        """Return the variable as an int."""
        return self._convert(key, default, lambda raw: int(raw.strip()), "integer")

    @overload
    def get_float(self, key: str) -> float: ...
    @overload
    def get_float(self, key: str, default: DefaultT) -> float | DefaultT: ...
    def get_float(self, key: str, default: Any = MISSING) -> Any:
        """Return the variable as a float."""
        return self._convert(key, default, lambda raw: float(raw.strip()), "number")

    @overload
    def get_bool(self, key: str) -> bool: ...
    @overload
    def get_bool(self, key: str, default: DefaultT) -> bool | DefaultT: ...
    def get_bool(self, key: str, default: Any = MISSING) -> Any:
        """Return the variable as a bool: 1/0, true/false, yes/no, on/off."""
        return self._convert(key, default, _to_bool, "boolean")

    @overload
    def get_list(self, key: str, *, separator: str = ",") -> list[str]: ...
    @overload
    def get_list(self, key: str, default: DefaultT, *, separator: str = ",") -> list[str] | DefaultT: ...
    def get_list(self, key: str, default: Any = MISSING, *, separator: str = ",") -> Any:
        """Split the variable on `separator`, dropping blanks."""
        return self._convert(
            key, default, lambda raw: [item.strip() for item in raw.split(separator) if item.strip()], "list"
        )

    def get_json(self, key: str, default: Any = MISSING) -> Any:
        """Parse the variable as JSON."""
        return self._convert(key, default, json.loads, "JSON value")

    def secret(self, key: str) -> Secret:
        """Return the variable wrapped in `Secret`, which masks it when printed."""
        return Secret(self.get(key))

    def require(self, *keys: str) -> None:
        """Raise one MissingSecretError naming every key that is unset or empty."""
        missing = self._environment.missing(*keys)
        if missing:
            raise MissingSecretError(missing)

    def database_url(self, name: str = "DATABASE_URL") -> str:
        """Connection URL injected by a database connection."""
        return self.get(name)

    def redis_url(self, name: str = "REDIS_URL") -> str:
        """Connection URL injected by a Redis connection."""
        return self.get(name)

    def is_runsite(self) -> bool:
        """True inside a container started by Runsite."""
        return not self._environment.missing("APP_NAME", "APP_PORT")
