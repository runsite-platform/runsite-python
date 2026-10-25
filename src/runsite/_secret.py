from __future__ import annotations

import hmac

MASK = "********"


class Secret:
    """A string that never shows its value in logs, reprs or f-strings."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret('{MASK}')"

    def __str__(self) -> str:
        return MASK

    def __format__(self, format_spec: str) -> str:
        return MASK

    def __eq__(self, other: object) -> bool:
        # compare_digest only accepts ASCII str, so compare the UTF-8 bytes.
        return isinstance(other, Secret) and hmac.compare_digest(self._value.encode(), other._value.encode())

    def __hash__(self) -> int:
        return hash((Secret, self._value))
