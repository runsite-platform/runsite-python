from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Iterable
from os import PathLike
from typing import Any

from runsite._storage.bucket import MAX_PRESIGN_SECONDS, Bucket, UploadSource
from runsite._storage.models import ObjectInfo

LIST_BATCH_SIZE = 1000
DEFAULT_CHUNK_BYTES = 1024 * 1024


class AsyncObjectStream:
    """Streaming body of an object; each read runs in a worker thread."""

    def __init__(self, body: Any) -> None:
        self._body = body

    async def read(self, amount: int | None = None) -> bytes:
        """Read `amount` bytes, or the rest of the object when it is None."""
        if amount is None:
            whole: bytes = await asyncio.to_thread(self._body.read)
            return whole
        chunk: bytes = await asyncio.to_thread(self._body.read, amount)
        return chunk

    async def iter_chunks(self, chunk_size: int = DEFAULT_CHUNK_BYTES) -> AsyncIterator[bytes]:
        while chunk := await self.read(chunk_size):
            yield chunk

    async def close(self) -> None:
        await asyncio.to_thread(self._body.close)


class AsyncBucket:
    """Async view of a `Bucket`: every network call runs in a worker thread, so the event loop never blocks.

    It shares the boto3 client and all behaviour with the sync `Bucket`; `sync` returns that bucket.
    """

    def __init__(self, bucket: Bucket) -> None:
        self.sync = bucket

    @property
    def name(self) -> str:
        return self.sync.name

    @property
    def client(self) -> Any:
        """The raw (synchronous) boto3 client."""
        return self.sync.client

    async def upload(self, key: str, source: UploadSource, *, content_type: str | None = None) -> None:
        """See `Bucket.upload`."""
        await asyncio.to_thread(self.sync.upload, key, source, content_type=content_type)

    async def open(self, key: str) -> AsyncObjectStream:
        """Return the object's streaming body; read it with `await stream.read()` or `stream.iter_chunks()`."""
        return AsyncObjectStream(await asyncio.to_thread(self.sync.open, key))

    async def download(self, key: str) -> bytes:
        """See `Bucket.download`."""
        return await asyncio.to_thread(self.sync.download, key)

    async def download_to(self, key: str, path: str | PathLike[str]) -> None:
        """See `Bucket.download_to`."""
        await asyncio.to_thread(self.sync.download_to, key, path)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self.sync.exists, key)

    async def list(self, prefix: str = "") -> AsyncIterator[ObjectInfo]:
        """Yield every object under `prefix`, fetching up to 1000 at a time in a worker thread."""
        objects = self.sync.list(prefix)
        while batch := await asyncio.to_thread(lambda: list(itertools.islice(objects, LIST_BATCH_SIZE))):
            for item in batch:
                yield item

    async def delete(self, key: str) -> None:
        """See `Bucket.delete`."""
        await asyncio.to_thread(self.sync.delete, key)

    async def delete_many(self, keys: Iterable[str]) -> None:
        """See `Bucket.delete_many`."""
        await asyncio.to_thread(self.sync.delete_many, keys)

    async def copy(self, source_key: str, target_key: str) -> None:
        """See `Bucket.copy`."""
        await asyncio.to_thread(self.sync.copy, source_key, target_key)

    def presigned_url(self, key: str, *, method: str = "GET", expires: int = MAX_PRESIGN_SECONDS) -> str:
        """See `Bucket.presigned_url`; signing is local, so this stays synchronous."""
        return self.sync._presigned_url(key, method, expires, stacklevel=3)

    def public_url(self, key: str) -> str:
        return self.sync.public_url(key)
