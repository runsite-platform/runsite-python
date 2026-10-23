from __future__ import annotations

import uuid
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

from runsite._http import AsyncTransport, RequestSettings, SyncTransport
from runsite._platform import platform_settings
from runsite._storage.bucket import Bucket, cached_bucket
from runsite._storage.models import AccessKey, AccessKeyInfo, BucketInfo

DELETE_BUCKET_TIMEOUT_SECONDS = 120.0


def bucket_path(name: str) -> str:
    return f"/storage/buckets/{quote(name, safe='')}"


def key_path(key_id: str | uuid.UUID) -> str:
    try:
        canonical_id = str(uuid.UUID(str(key_id)))
    except ValueError as error:
        raise ValueError(f"key_id must be a UUID such as AccessKeyInfo.id, got {key_id!r}") from error
    return f"/storage/keys/{canonical_id}"


def check_confirmation(name: str, confirm: str) -> None:
    if confirm != name:
        raise ValueError(f"Deleting a bucket removes every object in it; pass confirm={name!r} to proceed")


def key_body(*, bucket_id: str | None, name: str | None, read: bool, write: bool, delete: bool) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if bucket_id is not None:
        body["bucket_id"] = bucket_id
    body["permissions"] = {"read": read, "write": write, "delete": delete}
    return body


class _StorageSettings:
    def __init__(self, *, api_key: str | None, base_url: str | None, timeout: float, max_retries: int) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries

    def settings(self, *, long_running: bool = False) -> RequestSettings:
        timeout = max(self.timeout, DELETE_BUCKET_TIMEOUT_SECONDS) if long_running else self.timeout
        return platform_settings(
            api_key=self.api_key, base_url=self.base_url, timeout=timeout, max_retries=self.max_retries
        )


class Storage:
    """Manages buckets and S3 access keys through the Runsite API (needs RUNSITE_API_KEY).

    Bucket names are global across Runsite: 409 on create can mean another account owns the name.
    Exceeding the plan's bucket quota returns 400 (BadRequestError).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._config = _StorageSettings(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)
        self._http_client = http_client
        self._transport: SyncTransport | None = None
        self._long_transport: SyncTransport | None = None
        self._buckets: dict[tuple[str, str | None], Bucket] = {}

    def bucket(self, name: str | None = None, *, access_key: AccessKey | None = None) -> Bucket:
        """Data-plane client for a bucket. Defaults to S3_BUCKET and the injected S3_* credentials.

        The boto3 client is created once per bucket name and key and reused on later calls.
        """
        return cached_bucket(self._buckets, name, access_key)

    def _client(self) -> SyncTransport:
        if self._transport is None:
            self._transport = SyncTransport(self._config.settings(), http_client=self._http_client)
        return self._transport

    def _long_client(self) -> SyncTransport:
        if self._long_transport is None:
            settings = self._config.settings(long_running=True)
            self._long_transport = SyncTransport(settings, http_client=self._http_client)
        return self._long_transport

    def create_bucket(self, name: str, *, public: bool = False) -> BucketInfo:
        """Create a bucket (needs a write-scope API key). Names are 3-63 characters and globally unique."""
        body = {"name": name, "is_public": public}
        return BucketInfo.model_validate(self._client().request("POST", "/storage/buckets", json=body))

    def list_buckets(self) -> list[BucketInfo]:
        listing = self._client().request("GET", "/storage/buckets")
        return [BucketInfo.model_validate(item) for item in listing["buckets"]]

    def get_bucket(self, name: str) -> BucketInfo:
        return BucketInfo.model_validate(self._client().request("GET", bucket_path(name)))

    def update_bucket(self, name: str, *, public: bool) -> BucketInfo:
        """Make a bucket public (anonymous reads) or private."""
        return BucketInfo.model_validate(self._client().request("PATCH", bucket_path(name), json={"is_public": public}))

    def delete_bucket(self, name: str, *, confirm: str) -> None:
        """Delete a bucket and all its objects (needs an admin-scope API key). `confirm` must equal `name`.

        The call returns after the server has emptied the bucket, so it uses a timeout of at least 120 s.
        """
        check_confirmation(name, confirm)
        self._long_client().request("DELETE", bucket_path(name))

    def create_key(
        self,
        *,
        bucket: str | BucketInfo | None = None,
        name: str | None = None,
        read: bool = True,
        write: bool = True,
        delete: bool = False,
    ) -> AccessKey:
        """Create an S3 access key for one bucket, or for the whole workspace when `bucket` is None.

        A workspace-wide key needs an admin-scope API key and the workspace ADMIN role, because it can
        create and delete buckets over S3; a bucket-scoped key needs a write-scope API key.
        Bucket-scoped keys can always read, whatever `read` says.
        """
        bucket_id = None
        if isinstance(bucket, BucketInfo):
            bucket_id = bucket.id
        elif bucket is not None:
            bucket_id = self.get_bucket(bucket).id
        body = key_body(bucket_id=bucket_id, name=name, read=read, write=write, delete=delete)
        return AccessKey.model_validate(self._client().request("POST", "/storage/keys", json=body))

    def list_keys(self, *, page: int = 1, size: int = 50) -> list[AccessKeyInfo]:
        listing = self._client().request("GET", "/storage/keys", params={"page": page, "size": size})
        return [AccessKeyInfo.model_validate(item) for item in listing["keys"]]

    def delete_key(self, key_id: str | uuid.UUID) -> None:
        """Revoke a key (needs an admin-scope API key). Running S3 clients may keep working for about 5 minutes."""
        self._client().request("DELETE", key_path(key_id))

    def close(self) -> None:
        for transport in (self._transport, self._long_transport):
            if transport is not None:
                transport.close()
        self._transport = None
        self._long_transport = None

    def __enter__(self) -> Storage:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()


class AsyncStorage:
    """Async twin of `Storage`'s control-plane methods; the docstrings on `Storage` apply here too."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = _StorageSettings(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)
        self._http_client = http_client
        self._transport: AsyncTransport | None = None
        self._long_transport: AsyncTransport | None = None
        self._buckets: dict[tuple[str, str | None], Bucket] = {}

    def bucket(self, name: str | None = None, *, access_key: AccessKey | None = None) -> Bucket:
        """Data-plane client for a bucket. Defaults to S3_BUCKET and the injected S3_* credentials.

        The boto3 client is created once per bucket name and key and reused on later calls.
        """
        return cached_bucket(self._buckets, name, access_key)

    def _client(self) -> AsyncTransport:
        if self._transport is None:
            self._transport = AsyncTransport(self._config.settings(), http_client=self._http_client)
        return self._transport

    def _long_client(self) -> AsyncTransport:
        if self._long_transport is None:
            settings = self._config.settings(long_running=True)
            self._long_transport = AsyncTransport(settings, http_client=self._http_client)
        return self._long_transport

    async def create_bucket(self, name: str, *, public: bool = False) -> BucketInfo:
        body = {"name": name, "is_public": public}
        return BucketInfo.model_validate(await self._client().request("POST", "/storage/buckets", json=body))

    async def list_buckets(self) -> list[BucketInfo]:
        listing = await self._client().request("GET", "/storage/buckets")
        return [BucketInfo.model_validate(item) for item in listing["buckets"]]

    async def get_bucket(self, name: str) -> BucketInfo:
        return BucketInfo.model_validate(await self._client().request("GET", bucket_path(name)))

    async def update_bucket(self, name: str, *, public: bool) -> BucketInfo:
        response = await self._client().request("PATCH", bucket_path(name), json={"is_public": public})
        return BucketInfo.model_validate(response)

    async def delete_bucket(self, name: str, *, confirm: str) -> None:
        check_confirmation(name, confirm)
        await self._long_client().request("DELETE", bucket_path(name))

    async def create_key(
        self,
        *,
        bucket: str | BucketInfo | None = None,
        name: str | None = None,
        read: bool = True,
        write: bool = True,
        delete: bool = False,
    ) -> AccessKey:
        bucket_id = None
        if isinstance(bucket, BucketInfo):
            bucket_id = bucket.id
        elif bucket is not None:
            bucket_id = (await self.get_bucket(bucket)).id
        body = key_body(bucket_id=bucket_id, name=name, read=read, write=write, delete=delete)
        return AccessKey.model_validate(await self._client().request("POST", "/storage/keys", json=body))

    async def list_keys(self, *, page: int = 1, size: int = 50) -> list[AccessKeyInfo]:
        listing = await self._client().request("GET", "/storage/keys", params={"page": page, "size": size})
        return [AccessKeyInfo.model_validate(item) for item in listing["keys"]]

    async def delete_key(self, key_id: str | uuid.UUID) -> None:
        await self._client().request("DELETE", key_path(key_id))

    async def aclose(self) -> None:
        for transport in (self._transport, self._long_transport):
            if transport is not None:
                await transport.aclose()
        self._transport = None
        self._long_transport = None

    async def __aenter__(self) -> AsyncStorage:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()
