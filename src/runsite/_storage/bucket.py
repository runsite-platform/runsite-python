from __future__ import annotations

import io
import warnings
from collections.abc import Iterable, Iterator
from os import PathLike
from pathlib import Path
from typing import IO, Any, TypeAlias
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from runsite._config import Environment, S3Credentials, resolve_bucket_name, s3_credentials
from runsite._errors import ConfigurationError, ObjectNotFoundError, StorageAccessDeniedError, StorageError
from runsite._storage.models import AccessKey, ObjectInfo

MULTIPART_THRESHOLD_BYTES = 8 * 1024 * 1024
MULTIPART_CHUNK_BYTES = 16 * 1024 * 1024
MAX_PRESIGN_SECONDS = 900
MAX_LIST_PAGES = 100_000
DELETE_BATCH_MAX_KEYS = 1000
DELETE_BATCH_MAX_BODY_BYTES = 200 * 1024
DELETE_REQUEST_OVERHEAD_BYTES = 128
DELETE_ENTRY_OVERHEAD_BYTES = 32
MAX_REPORTED_FAILURES = 10
NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})
ACCESS_DENIED_CODES = frozenset({"403", "AccessDenied", "Forbidden"})
PRESIGN_METHODS = {"GET": "get_object", "PUT": "put_object", "DELETE": "delete_object", "HEAD": "head_object"}
INSTALL_HINT = 'Object storage needs boto3: pip install "runsite-sdk[storage]"'
DELETE_DENIED_HINT = (
    "This key has no delete permission. Keys injected by a dashboard connection are read/write only; "
    "create one with storage.create_key(bucket=..., delete=True) and pass it as access_key=..."
)
STR_SOURCE_HINT = (
    "Upload source is not an existing file. A str or PathLike source is read as a file path; "
    "pass bytes (for example text.encode()) to upload content."
)

UploadSource: TypeAlias = "bytes | str | PathLike[str] | IO[bytes]"


def _boto3() -> Any:
    try:
        import boto3
    except ImportError as error:
        raise ConfigurationError(INSTALL_HINT) from error
    return boto3


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


def cached_bucket(
    cache: dict[tuple[str, str | None], Bucket],
    name: str | None,
    access_key: AccessKey | None,
    environment: Environment,
) -> Bucket:
    """Return the cached `Bucket` for this name and key, creating its boto3 client on first use."""
    resolved_name = resolve_bucket_name(environment, name)
    cache_key = (resolved_name, access_key.access_key if access_key is not None else None)
    bucket = cache.get(cache_key)
    if bucket is None:
        credentials = credentials_for(access_key, environment)
        bucket = Bucket(resolved_name, client=make_s3_client(credentials), endpoint_url=credentials.endpoint_url)
        cache[cache_key] = bucket
    return bucket


def _translate(error: Exception, *, denied_hint: str | None = None) -> StorageError:
    from botocore.exceptions import BotoCoreError, ClientError

    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", ""))
        message = str(error.response.get("Error", {}).get("Message", code)) or code
        if code in NOT_FOUND_CODES:
            return ObjectNotFoundError(message, code=code)
        if code in ACCESS_DENIED_CODES:
            return StorageAccessDeniedError(f"{message}. {denied_hint}" if denied_hint else message, code=code)
        return StorageError(message, code=code)
    if isinstance(error, BotoCoreError):
        return StorageError(str(error))
    raise error


def delete_batches(keys: Iterable[str]) -> Iterator[list[str]]:
    """Split keys into DeleteObjects requests of at most 1000 keys and about 200 KiB of XML each."""
    batch: list[str] = []
    batch_bytes = DELETE_REQUEST_OVERHEAD_BYTES
    for key in keys:
        entry_bytes = len(xml_escape(key).encode()) + DELETE_ENTRY_OVERHEAD_BYTES
        if batch and (len(batch) >= DELETE_BATCH_MAX_KEYS or batch_bytes + entry_bytes > DELETE_BATCH_MAX_BODY_BYTES):
            yield batch
            batch, batch_bytes = [], DELETE_REQUEST_OVERHEAD_BYTES
        batch.append(key)
        batch_bytes += entry_bytes
    if batch:
        yield batch


def delete_failure_error(failures: list[dict[str, Any]]) -> StorageError:
    codes = {str(failure.get("Code", "")) for failure in failures}
    shown = ", ".join(f"{failure.get('Key')} ({failure.get('Code')})" for failure in failures[:MAX_REPORTED_FAILURES])
    remaining = len(failures) - MAX_REPORTED_FAILURES
    suffix = f" and {remaining} more" if remaining > 0 else ""
    message = f"Could not delete {len(failures)} object(s): {shown}{suffix}"
    first_code = str(failures[0].get("Code", "")) or None
    if codes <= ACCESS_DENIED_CODES:
        return StorageAccessDeniedError(f"{message}. {DELETE_DENIED_HINT}", code=first_code)
    return StorageError(message, code=first_code)


class Bucket:
    """One bucket of Runsite object storage, on top of boto3. `client` exposes the raw boto3 client.

    Credentials injected by a dashboard connection (S3_*) can read and write but not delete.
    """

    def __init__(self, name: str, *, client: Any, endpoint_url: str) -> None:
        self.name = name
        self.client = client
        self._endpoint_url = endpoint_url.rstrip("/")

    def _call(self, operation: str, *, denied_hint: str | None = None, **arguments: Any) -> Any:
        try:
            return getattr(self.client, operation)(Bucket=self.name, **arguments)
        except Exception as error:
            raise _translate(error, denied_hint=denied_hint) from error

    def upload(self, key: str, source: UploadSource, *, content_type: str | None = None) -> None:
        """Upload bytes, a file path (str or PathLike) or a binary file object.

        Sources above 8 MiB use multipart upload in 16 MiB parts, up to about 30 GB per object. If a
        multipart upload fails with a key that cannot delete, already uploaded parts stay until the
        storage service cleans them up.
        """
        extra = {"ContentType": content_type} if content_type else {}
        if isinstance(source, bytes) and len(source) <= MULTIPART_THRESHOLD_BYTES:
            self._call("put_object", Key=key, Body=source, **extra)
            return
        from boto3.s3.transfer import TransferConfig

        config = TransferConfig(
            multipart_threshold=MULTIPART_THRESHOLD_BYTES, multipart_chunksize=MULTIPART_CHUNK_BYTES
        )
        if isinstance(source, bytes):
            stream: IO[bytes] = io.BytesIO(source)
        elif isinstance(source, (str, PathLike)):
            try:
                file = Path(source).open("rb")  # noqa: SIM115 - closed by the "with" below
            except FileNotFoundError as error:
                raise FileNotFoundError(STR_SOURCE_HINT) from error
            with file:
                self._upload_stream(file, key, extra, config)
            return
        else:
            stream = source
        self._upload_stream(stream, key, extra, config)

    def _upload_stream(self, stream: IO[bytes], key: str, extra: dict[str, str], config: Any) -> None:
        try:
            self.client.upload_fileobj(stream, self.name, key, ExtraArgs=extra or None, Config=config)
        except Exception as error:
            raise _translate(error) from error

    def open(self, key: str) -> Any:
        """Return the streaming body of an object; call `.read()` or iterate it."""
        return self._call("get_object", Key=key)["Body"]

    def download(self, key: str) -> bytes:
        """Read a whole object into memory; use `download_to` or `open` for large objects."""
        body: bytes = self.open(key).read()
        return body

    def download_to(self, key: str, path: str | PathLike[str]) -> None:
        """Stream an object into a file, overwriting it. A failed download removes the partial file."""
        target = Path(path)
        with target.open("wb") as file:
            try:
                self.client.download_fileobj(self.name, key, file)
            except Exception as error:
                file.close()
                target.unlink(missing_ok=True)
                raise _translate(error) from error

    def exists(self, key: str) -> bool:
        try:
            self._call("head_object", Key=key)
        except ObjectNotFoundError:
            return False
        return True

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """Yield every object under `prefix`, following pagination."""
        arguments: dict[str, Any] = {"Prefix": prefix}
        seen_tokens: set[str] = set()
        for _ in range(MAX_LIST_PAGES):
            page = self._call("list_objects_v2", **arguments)
            for item in page.get("Contents", []):
                yield ObjectInfo(
                    key=item["Key"],
                    size=item.get("Size", 0),
                    etag=str(item.get("ETag", "")).strip('"') or None,
                    last_modified=item.get("LastModified"),
                )
            if not page.get("IsTruncated"):
                return
            token = page.get("NextContinuationToken")
            if not token or token in seen_tokens:
                raise StorageError("Listing returned a truncated page without a new continuation token")
            seen_tokens.add(token)
            arguments["ContinuationToken"] = token
        raise StorageError(f"Listing stopped after {MAX_LIST_PAGES} pages")

    def delete(self, key: str) -> None:
        """Delete one object; needs a key with delete permission (connection-injected keys have none)."""
        self._call("delete_object", denied_hint=DELETE_DENIED_HINT, Key=key)

    def delete_many(self, keys: Iterable[str]) -> None:
        """Delete objects in batches; raises StorageError naming the keys the server could not delete."""
        failures: list[dict[str, Any]] = []
        for batch in delete_batches(keys):
            objects = [{"Key": key} for key in batch]
            response = self._call(
                "delete_objects", denied_hint=DELETE_DENIED_HINT, Delete={"Objects": objects, "Quiet": True}
            )
            failures.extend(response.get("Errors", []))
        if failures:
            raise delete_failure_error(failures)

    def copy(self, source_key: str, target_key: str) -> None:
        """Server-side copy within this bucket; objects up to 5 GB (no multipart copy)."""
        self._call("copy_object", Key=target_key, CopySource={"Bucket": self.name, "Key": source_key})

    def presigned_url(self, key: str, *, method: str = "GET", expires: int = MAX_PRESIGN_SECONDS) -> str:
        """A URL for GET/PUT/DELETE/HEAD without credentials.

        Runsite does not enforce `expires`: every presigned URL works for about 15 minutes after signing.
        Values above 900 are capped and values below 900 trigger a warning; both are UserWarning.
        """
        operation = PRESIGN_METHODS.get(method.upper())
        if operation is None:
            raise ValueError(f"method must be one of {sorted(PRESIGN_METHODS)}")
        if expires <= 0:
            raise ValueError(f"expires must be a positive number of seconds, got {expires}")
        if expires > MAX_PRESIGN_SECONDS:
            warnings.warn(
                f"Runsite presigned URLs are valid for at most {MAX_PRESIGN_SECONDS} seconds; expires was capped",
                stacklevel=2,
            )
            expires = MAX_PRESIGN_SECONDS
        elif expires < MAX_PRESIGN_SECONDS:
            warnings.warn(
                f"expires={expires} is not enforced by Runsite; the URL works for about {MAX_PRESIGN_SECONDS} seconds",
                stacklevel=2,
            )
        try:
            url: str = self.client.generate_presigned_url(
                ClientMethod=operation, Params={"Bucket": self.name, "Key": key}, ExpiresIn=expires
            )
        except Exception as error:
            raise _translate(error) from error
        return url

    def public_url(self, key: str) -> str:
        """Anonymous URL of an object; works only when the bucket is public."""
        return f"{self._endpoint_url}/{quote(self.name)}/{quote(key)}"
