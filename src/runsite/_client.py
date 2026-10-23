from __future__ import annotations

from types import TracebackType
from typing import TypeVar

import httpx

from runsite._http import AsyncTransport, Operation, RequestSettings, SyncTransport

ResultT = TypeVar("ResultT")
SyncClientT = TypeVar("SyncClientT", bound="SyncClient")
AsyncClientT = TypeVar("AsyncClientT", bound="AsyncClient")


class SyncClient:
    """Lifecycle shared by the sync clients: a lazy connection pool, `close()` and `with` support.

    Subclasses provide `_request_settings`, which is called on the first request (so configuration errors
    surface then, not in the constructor) and again after `close()`.
    """

    def __init__(self, *, http_client: httpx.Client | None) -> None:
        self._transport = SyncTransport(self._request_settings, http_client=http_client)

    def _request_settings(self) -> RequestSettings:
        raise NotImplementedError

    def _run(self, operation: Operation[ResultT]) -> ResultT:
        return self._transport.run(operation)

    def close(self) -> None:
        """Close the connection pool. The client can still be used; it opens a new pool."""
        self._transport.close()

    def __enter__(self: SyncClientT) -> SyncClientT:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()


class AsyncClient:
    """Async twin of `SyncClient`."""

    def __init__(self, *, http_client: httpx.AsyncClient | None) -> None:
        self._transport = AsyncTransport(self._request_settings, http_client=http_client)

    def _request_settings(self) -> RequestSettings:
        raise NotImplementedError

    async def _run(self, operation: Operation[ResultT]) -> ResultT:
        return await self._transport.run(operation)

    async def aclose(self) -> None:
        """Close the connection pool. The client can still be used; it opens a new pool."""
        await self._transport.aclose()

    async def __aenter__(self: AsyncClientT) -> AsyncClientT:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()
