"""Turns a bucket name and optional access key into a connected `Bucket`, once per name and key."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runsite._config import Environment, S3Credentials, resolve_bucket_name, s3_credentials
from runsite._errors import ConfigurationError
from runsite._storage.async_bucket import AsyncBucket
from runsite._storage.bucket import Bucket
from runsite._storage.models import AccessKey

INSTALL_HINT = 'Object storage needs boto3: pip install "runsite-sdk[storage]"'

S3ClientFactory = Callable[[S3Credentials], Any]
BucketCacheKey = tuple[str, "str | None"]


def _boto3() -> Any:
    try:
        import boto3
    except ImportError as error:
        raise ConfigurationError(INSTALL_HINT) from error
    return boto3


def make_s3_client(credentials: S3Credentials) -> Any:
    """Build a boto3 S3 client for Runsite storage."""
    boto3 = _boto3()
    from botocore.config import Config

    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return boto3.client(
        "s3",
        endpoint_url=credentials.endpoint_url,
        region_name=credentials.region,
        aws_access_key_id=credentials.key_id,
        aws_secret_access_key=credentials.secret,
        config=config,
    )


def credentials_for(access_key: AccessKey | None, environment: Environment) -> S3Credentials:
    """Credentials of a created key, else the ones injected into the environment."""
    if access_key is None:
        return s3_credentials(environment)
    return S3Credentials(
        key_id=access_key.access_key,
        secret=access_key.secret_key.reveal(),
        endpoint_url=access_key.endpoint_url,
        region=access_key.region,
    )


class BucketRegistry:
    """Hands out one `Bucket` (and one `AsyncBucket` view of it) per bucket name and access key.

    The bucket name defaults to S3_BUCKET and the credentials to the injected S3_* variables. `close()`
    closes the connection pools of every boto3 client; the buckets stay usable and reconnect on demand.
    """

    def __init__(self, environment: Environment, *, client_factory: S3ClientFactory = make_s3_client) -> None:
        self._environment = environment
        self._client_factory = client_factory
        self._buckets: dict[BucketCacheKey, Bucket] = {}
        self._async_buckets: dict[BucketCacheKey, AsyncBucket] = {}

    def _cache_key(self, name: str | None, access_key: AccessKey | None) -> BucketCacheKey:
        resolved_name = resolve_bucket_name(self._environment, name)
        return resolved_name, access_key.access_key if access_key is not None else None

    def _bucket_for(self, cache_key: BucketCacheKey, access_key: AccessKey | None) -> Bucket:
        bucket = self._buckets.get(cache_key)
        if bucket is None:
            credentials = credentials_for(access_key, self._environment)
            client = self._client_factory(credentials)
            bucket = self._buckets[cache_key] = Bucket(
                cache_key[0], client=client, endpoint_url=credentials.endpoint_url
            )
        return bucket

    def bucket(self, name: str | None, access_key: AccessKey | None) -> Bucket:
        return self._bucket_for(self._cache_key(name, access_key), access_key)

    def async_bucket(self, name: str | None, access_key: AccessKey | None) -> AsyncBucket:
        cache_key = self._cache_key(name, access_key)
        async_bucket = self._async_buckets.get(cache_key)
        if async_bucket is None:
            async_bucket = self._async_buckets[cache_key] = AsyncBucket(self._bucket_for(cache_key, access_key))
        return async_bucket

    def close(self) -> None:
        for bucket in self._buckets.values():
            bucket.client.close()
