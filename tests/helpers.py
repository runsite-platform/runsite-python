from typing import Any

from runsite._http import Operation, Request


def single(request: Request) -> Operation[Any]:
    """An operation that sends one request and returns its parsed body."""
    return (yield request)
