from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from runsite._client import AsyncClient, SyncClient
from runsite._config import Environment, platform_settings
from runsite._http import Operation, Request, RequestSettings
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


class _StorageOperations:
    """Control-plane operations shared by `Storage` and `AsyncStorage`."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
        max_retries: int,
        environ: Mapping[str, str] | None,
    ) -> None:
        self._environment = Environment(environ)
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._max_retries = max_retries
        self._buckets: dict[tuple[str, str | None], Bucket] = {}

    def _request_settings(self) -> RequestSettings:
        return platform_settings(
            self._environment,
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            max_retries=self._max_retries,
        )

    def bucket(self, name: str | None = None, *, access_key: AccessKey | None = None) -> Bucket:
        """Data-plane client for a bucket. Defaults to S3_BUCKET and the injected S3_* credentials.

        The boto3 client is created once per bucket name and key and reused on later calls.
        """
        return cached_bucket(self._buckets, name, access_key, self._environment)

    def _create_bucket(self, name: str, public: bool) -> Operation[BucketInfo]:
        body = {"name": name, "is_public": public}
        return BucketInfo.model_validate((yield Request("POST", "/storage/buckets", json=body)))

    def _list_buckets(self) -> Operation[list[BucketInfo]]:
        listing = yield Request("GET", "/storage/buckets")
        return [BucketInfo.model_validate(item) for item in listing["buckets"]]

    def _get_bucket(self, name: str) -> Operation[BucketInfo]:
        return BucketInfo.model_validate((yield Request("GET", bucket_path(name))))

    def _update_bucket(self, name: str, public: bool) -> Operation[BucketInfo]:
        return BucketInfo.model_validate((yield Request("PATCH", bucket_path(name), json={"is_public": public})))

    def _delete_bucket(self, name: str, confirm: str) -> Operation[None]:
        check_confirmation(name, confirm)
        yield Request("DELETE", bucket_path(name), min_timeout=DELETE_BUCKET_TIMEOUT_SECONDS)

    def _create_key(
        self, bucket: str | BucketInfo | None, name: str | None, read: bool, write: bool, delete: bool
    ) -> Operation[AccessKey]:
        bucket_id = None
        if isinstance(bucket, BucketInfo):
            bucket_id = bucket.id
        elif bucket is not None:
            bucket_id = (yield from self._get_bucket(bucket)).id
        body = key_body(bucket_id=bucket_id, name=name, read=read, write=write, delete=delete)
        return AccessKey.model_validate((yield Request("POST", "/storage/keys", json=body)))

    def _list_keys(self, page: int, size: int) -> Operation[list[AccessKeyInfo]]:
        listing = yield Request("GET", "/storage/keys", params={"page": page, "size": size})
        return [AccessKeyInfo.model_validate(item) for item in listing["keys"]]

    def _delete_key(self, key_id: str | uuid.UUID) -> Operation[None]:
        yield Request("DELETE", key_path(key_id))


class Storage(_StorageOperations, SyncClient):
    """Manages buckets and S3 access keys through the Runsite API (needs RUNSITE_API_KEY).

    Settings not passed to the constructor come from `environ=` when given, else from `os.environ`.

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
        environ: Mapping[str, str] | None = None,
    ) -> None:
        _StorageOperations.__init__(
            self, api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries, environ=environ
        )
        SyncClient.__init__(self, http_client=http_client)

    def create_bucket(self, name: str, *, public: bool = False) -> BucketInfo:
        """Create a bucket (needs a write-scope API key). Names are 3-63 characters and globally unique."""
        return self._run(self._create_bucket(name, public))

    def list_buckets(self) -> list[BucketInfo]:
        return self._run(self._list_buckets())

    def get_bucket(self, name: str) -> BucketInfo:
        return self._run(self._get_bucket(name))

    def update_bucket(self, name: str, *, public: bool) -> BucketInfo:
        """Make a bucket public (anonymous reads) or private."""
        return self._run(self._update_bucket(name, public))

    def delete_bucket(self, name: str, *, confirm: str) -> None:
        """Delete a bucket and all its objects (needs an admin-scope API key). `confirm` must equal `name`.

        The call returns after the server has emptied the bucket, so it uses a timeout of at least 120 s.
        """
        self._run(self._delete_bucket(name, confirm))

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
        return self._run(self._create_key(bucket, name, read, write, delete))

    def list_keys(self, *, page: int = 1, size: int = 50) -> list[AccessKeyInfo]:
        return self._run(self._list_keys(page, size))

    def delete_key(self, key_id: str | uuid.UUID) -> None:
        """Revoke a key (needs an admin-scope API key). Running S3 clients may keep working for about 5 minutes."""
        self._run(self._delete_key(key_id))


class AsyncStorage(_StorageOperations, AsyncClient):
    """Async twin of `Storage`'s control-plane methods; the docstrings on `Storage` apply here too."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        http_client: httpx.AsyncClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        _StorageOperations.__init__(
            self, api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries, environ=environ
        )
        AsyncClient.__init__(self, http_client=http_client)

    async def create_bucket(self, name: str, *, public: bool = False) -> BucketInfo:
        return await self._run(self._create_bucket(name, public))

    async def list_buckets(self) -> list[BucketInfo]:
        return await self._run(self._list_buckets())

    async def get_bucket(self, name: str) -> BucketInfo:
        return await self._run(self._get_bucket(name))

    async def update_bucket(self, name: str, *, public: bool) -> BucketInfo:
        return await self._run(self._update_bucket(name, public))

    async def delete_bucket(self, name: str, *, confirm: str) -> None:
        await self._run(self._delete_bucket(name, confirm))

    async def create_key(
        self,
        *,
        bucket: str | BucketInfo | None = None,
        name: str | None = None,
        read: bool = True,
        write: bool = True,
        delete: bool = False,
    ) -> AccessKey:
        return await self._run(self._create_key(bucket, name, read, write, delete))

    async def list_keys(self, *, page: int = 1, size: int = 50) -> list[AccessKeyInfo]:
        return await self._run(self._list_keys(page, size))

    async def delete_key(self, key_id: str | uuid.UUID) -> None:
        await self._run(self._delete_key(key_id))
