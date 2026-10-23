import io
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from runsite._config import Environment, s3_credentials
from runsite._errors import ConfigurationError, ObjectNotFoundError, StorageAccessDeniedError, StorageError
from runsite._storage.async_bucket import AsyncBucket
from runsite._storage.bucket import (
    DELETE_BATCH_MAX_BODY_BYTES,
    MAX_PRESIGN_SECONDS,
    MULTIPART_CHUNK_BYTES,
    Bucket,
)
from runsite._storage.control import AsyncStorage, Storage
from runsite._storage.models import AccessKey
from runsite._storage.registry import BucketRegistry, make_s3_client

AWS_VARIABLES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_S3",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_REQUEST_CHECKSUM_CALCULATION",
    "AWS_RESPONSE_CHECKSUM_VALIDATION",
)


@pytest.fixture(autouse=True)
def clear_aws_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AWS_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def client_error(code: str, operation: str = "GetObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)

    def read(self) -> bytes:
        return self._stream.read()


class FakeS3Client:
    def __init__(
        self,
        list_pages: list[dict[str, Any]] | None = None,
        error: ClientError | None = None,
        delete_errors: list[dict[str, str]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._list_pages = list(list_pages or [])
        self._error = error
        self._delete_errors = list(delete_errors or [])
        self.objects: dict[str, bytes] = {"a.txt": b"hello"}
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        if self._error is not None:
            raise self._error

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("put_object", **kwargs)
        return {}

    def upload_fileobj(self, fileobj: Any, bucket: str, key: str, ExtraArgs: Any = None, Config: Any = None) -> None:
        self._record("upload_fileobj", bucket=bucket, key=key, extra=ExtraArgs, config=Config, data=fileobj.read())

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_object", **kwargs)
        return {"Body": FakeBody(self.objects[kwargs["Key"]])}

    def download_fileobj(self, bucket: str, key: str, fileobj: Any, ExtraArgs: Any = None, Config: Any = None) -> None:
        self._record("download_fileobj", bucket=bucket, key=key)
        if key not in self.objects:
            fileobj.write(b"partial")
            raise client_error("404", "HeadObject")
        fileobj.write(self.objects[key])

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("head_object", **kwargs)
        if kwargs["Key"] not in self.objects:
            raise client_error("404", "HeadObject")
        return {}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_object", **kwargs)
        return {}

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_objects", **kwargs)
        errors, self._delete_errors = self._delete_errors, []
        return {"Errors": errors} if errors else {}

    def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("copy_object", **kwargs)
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_objects_v2", **kwargs)
        return self._list_pages.pop(0)

    def generate_presigned_url(self, ClientMethod: str, Params: dict[str, Any], ExpiresIn: int) -> str:
        self.calls.append(("generate_presigned_url", {"method": ClientMethod, "params": Params, "expires": ExpiresIn}))
        return "https://s3.runsite.test/signed"


def make_bucket(client: FakeS3Client | None = None) -> tuple[Bucket, FakeS3Client]:
    fake = client or FakeS3Client()
    return Bucket("avatars", client=fake, endpoint_url="https://s3.runsite.test"), fake


RUNSITE_VARIABLES = {
    "S3_ACCESS_KEY_ID": "AKIA1",
    "S3_SECRET_ACCESS_KEY": "secret",
    "S3_ENDPOINT_URL": "https://s3.runsite.test",
}


def set_runsite_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in RUNSITE_VARIABLES.items():
        monkeypatch.setenv(name, value)


def test_upload_bytes_uses_put_object() -> None:
    bucket, fake = make_bucket()

    bucket.upload("a.png", b"data", content_type="image/png")

    assert fake.calls == [
        ("put_object", {"Bucket": "avatars", "Key": "a.png", "Body": b"data", "ContentType": "image/png"})
    ]


def test_upload_path_uses_managed_transfer(tmp_path: Path) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"from disk")
    bucket, fake = make_bucket()

    bucket.upload("file.bin", source)

    name, arguments = fake.calls[0]
    assert name == "upload_fileobj"
    assert arguments["data"] == b"from disk"
    assert arguments["config"].multipart_chunksize == MULTIPART_CHUNK_BYTES


def test_upload_missing_path_explains_str_sources() -> None:
    bucket, fake = make_bucket()

    with pytest.raises(FileNotFoundError, match="pass bytes"):
        bucket.upload("note.txt", "hello world")
    assert fake.calls == []


def test_download_and_download_to(tmp_path: Path) -> None:
    bucket, fake = make_bucket()
    target = tmp_path / "out.txt"

    assert bucket.download("a.txt") == b"hello"
    bucket.download_to("a.txt", target)

    assert target.read_bytes() == b"hello"
    assert fake.calls[-1] == ("download_fileobj", {"bucket": "avatars", "key": "a.txt"})


def test_failed_download_to_removes_partial_file(tmp_path: Path) -> None:
    bucket, _ = make_bucket()
    target = tmp_path / "out.txt"

    with pytest.raises(ObjectNotFoundError):
        bucket.download_to("missing.txt", target)
    assert not target.exists()


def test_exists() -> None:
    bucket, _ = make_bucket()

    assert bucket.exists("a.txt") is True
    assert bucket.exists("missing.txt") is False


def test_exists_raises_on_access_denied() -> None:
    bucket, _ = make_bucket(FakeS3Client(error=client_error("AccessDenied", "HeadObject")))

    with pytest.raises(StorageAccessDeniedError):
        bucket.exists("a.txt")


def test_list_follows_continuation_tokens() -> None:
    moment = datetime(2026, 10, 22, tzinfo=timezone.utc)
    pages = [
        {
            "Contents": [{"Key": "a/1", "Size": 1, "ETag": '"e1"', "LastModified": moment}],
            "IsTruncated": True,
            "NextContinuationToken": "t1",
        },
        {"Contents": [{"Key": "a/2", "Size": 2, "ETag": '"e2"', "LastModified": moment}], "IsTruncated": False},
    ]
    bucket, fake = make_bucket(FakeS3Client(list_pages=pages))

    keys = [item.key for item in bucket.list(prefix="a/")]

    assert keys == ["a/1", "a/2"]
    assert [arguments["Prefix"] for _, arguments in fake.calls] == ["a/", "a/"]
    assert fake.calls[1][1]["ContinuationToken"] == "t1"


def test_list_empty_bucket() -> None:
    bucket, _ = make_bucket(FakeS3Client(list_pages=[{"KeyCount": 0, "IsTruncated": False}]))

    assert list(bucket.list()) == []


def test_list_truncated_page_without_token_raises() -> None:
    bucket, _ = make_bucket(FakeS3Client(list_pages=[{"Contents": [], "IsTruncated": True}]))

    with pytest.raises(StorageError, match="continuation token"):
        list(bucket.list())


def test_list_repeated_token_raises() -> None:
    page = {"Contents": [], "IsTruncated": True, "NextContinuationToken": "t1"}
    bucket, fake = make_bucket(FakeS3Client(list_pages=[page, page]))

    with pytest.raises(StorageError, match="continuation token"):
        list(bucket.list())
    assert len(fake.calls) == 2


def test_delete_many_batches_by_thousand() -> None:
    bucket, fake = make_bucket()
    keys = [f"k{index}" for index in range(1001)]

    bucket.delete_many(keys)

    batches = [arguments["Delete"]["Objects"] for name, arguments in fake.calls if name == "delete_objects"]
    assert [len(batch) for batch in batches] == [1000, 1]


def test_delete_many_batches_by_body_size() -> None:
    bucket, fake = make_bucket()
    keys = [f"{index:04d}" + "x" * 996 for index in range(300)]

    bucket.delete_many(keys)

    batches = [arguments["Delete"]["Objects"] for name, arguments in fake.calls if name == "delete_objects"]
    assert [len(batch) for batch in batches] == [198, 102]
    assert all(len(batch) * 1032 + 128 <= DELETE_BATCH_MAX_BODY_BYTES for batch in batches)


def test_delete_many_with_no_keys_sends_nothing() -> None:
    bucket, fake = make_bucket()

    bucket.delete_many([])

    assert fake.calls == []


def test_delete_many_reports_failed_keys() -> None:
    errors = [{"Key": "k1", "Code": "InternalError", "Message": "boom"}]
    bucket, _ = make_bucket(FakeS3Client(delete_errors=errors))

    with pytest.raises(StorageError, match="k1") as caught:
        bucket.delete_many(["k1", "k2"])
    assert caught.value.code == "InternalError"


def test_delete_many_access_denied_explains_connection_keys() -> None:
    errors = [{"Key": "k1", "Code": "AccessDenied", "Message": "denied"}]
    bucket, _ = make_bucket(FakeS3Client(delete_errors=errors))

    with pytest.raises(StorageAccessDeniedError, match="delete=True"):
        bucket.delete_many(["k1"])


def test_delete_access_denied_explains_connection_keys() -> None:
    bucket, _ = make_bucket(FakeS3Client(error=client_error("AccessDenied", "DeleteObject")))

    with pytest.raises(StorageAccessDeniedError, match="delete=True"):
        bucket.delete("a.txt")


def test_copy() -> None:
    bucket, fake = make_bucket()

    bucket.copy("a.txt", "b.txt")

    assert fake.calls == [
        ("copy_object", {"Bucket": "avatars", "Key": "b.txt", "CopySource": {"Bucket": "avatars", "Key": "a.txt"}})
    ]


def test_presigned_url_default_has_no_warning() -> None:
    bucket, fake = make_bucket()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        bucket.presigned_url("a.txt")

    assert fake.calls[0][1]["expires"] == MAX_PRESIGN_SECONDS


def test_presigned_url_is_capped_with_warning() -> None:
    bucket, fake = make_bucket()

    with pytest.warns(UserWarning, match="900"):
        bucket.presigned_url("a.txt", expires=3600)

    assert fake.calls[0][1]["expires"] == MAX_PRESIGN_SECONDS


def test_short_presigned_url_warns_that_it_is_not_enforced() -> None:
    bucket, fake = make_bucket()

    with pytest.warns(UserWarning, match="not enforced") as caught:
        bucket.presigned_url("a.txt", method="PUT", expires=60)

    assert caught[0].filename == __file__
    assert fake.calls[0][1]["method"] == "put_object"
    assert fake.calls[0][1]["expires"] == 60


@pytest.mark.parametrize("expires", [0, -5])
def test_presigned_url_rejects_non_positive_expiry(expires: int) -> None:
    bucket, _ = make_bucket()

    with pytest.raises(ValueError, match="expires"):
        bucket.presigned_url("a.txt", expires=expires)


def test_public_url_is_path_style() -> None:
    bucket, _ = make_bucket()

    assert bucket.public_url("photos/me 1.png") == "https://s3.runsite.test/avatars/photos/me%201.png"


@pytest.mark.parametrize(
    ("code", "error_class"),
    [
        ("NoSuchKey", ObjectNotFoundError),
        ("NoSuchBucket", ObjectNotFoundError),
        ("AccessDenied", StorageAccessDeniedError),
        ("InternalError", StorageError),
    ],
)
def test_client_errors_are_mapped(code: str, error_class: type[StorageError]) -> None:
    bucket, _ = make_bucket(FakeS3Client(error=client_error(code)))

    with pytest.raises(error_class) as caught:
        bucket.download("a.txt")
    assert caught.value.code == code
    assert isinstance(caught.value.__cause__, ClientError)


def test_make_s3_client_from_environment() -> None:
    credentials = s3_credentials(Environment({**RUNSITE_VARIABLES, "S3_REGION": "eu-central"}))

    client = make_s3_client(credentials)

    assert client.meta.region_name == "eu-central"
    assert client.meta.config.s3["addressing_style"] == "path"
    assert client.meta.config.request_checksum_calculation == "when_required"
    assert client.meta.config.response_checksum_validation == "when_required"
    with Stubber(client) as stubber:
        stubber.add_response("head_object", {}, {"Bucket": "avatars", "Key": "a.txt"})
        Bucket("avatars", client=client, endpoint_url=credentials.endpoint_url).exists("a.txt")
        stubber.assert_no_pending_responses()


def test_storage_reads_bucket_credentials_from_injected_mapping() -> None:
    storage = Storage(environ={**RUNSITE_VARIABLES, "S3_BUCKET": "uploads"})

    assert storage.bucket().name == "uploads"


def test_missing_credentials_name_the_variable() -> None:
    with pytest.raises(ConfigurationError, match="S3_ACCESS_KEY_ID"):
        Storage(environ={}).bucket("avatars")


def test_storage_bucket_uses_s3_bucket_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    set_runsite_variables(monkeypatch)
    monkeypatch.setenv("S3_BUCKET", "uploads")

    assert Storage().bucket().name == "uploads"


def test_storage_bucket_is_cached_per_name(monkeypatch: pytest.MonkeyPatch) -> None:
    set_runsite_variables(monkeypatch)
    storage = Storage()

    first = storage.bucket("uploads")

    assert storage.bucket("uploads") is first
    assert storage.bucket("other") is not first


def test_storage_bucket_without_name_or_variable() -> None:
    with pytest.raises(ConfigurationError, match="S3_BUCKET"):
        Storage().bucket()


def created_key(access_key: str = "AKIA3") -> AccessKey:
    return AccessKey.model_validate(
        {
            "id": "9a1c2b3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d",
            "access_key": access_key,
            "secret_key": "sk-test-value",
            "permissions": {"read": True, "write": True, "delete": True},
            "bucket_id": "5b0f6d2a-3c1e-4f8a-9d7b-2e6c1a4b8f00",
            "endpoint_url": "https://s3.runsite.test",
            "region": "eu-central",
        }
    )


def test_bucket_from_created_key() -> None:
    bucket = Storage().bucket("avatars", access_key=created_key())

    assert bucket.client.meta.region_name == "eu-central"


def test_storage_bucket_is_cached_per_access_key(monkeypatch: pytest.MonkeyPatch) -> None:
    set_runsite_variables(monkeypatch)
    storage = Storage()
    first_key, second_key = created_key("AKIA3"), created_key("AKIA4")

    with_first_key = storage.bucket("avatars", access_key=first_key)

    assert storage.bucket("avatars", access_key=first_key) is with_first_key
    assert storage.bucket("avatars", access_key=second_key) is not with_first_key
    assert storage.bucket("avatars") is not with_first_key


def make_async_bucket(client: FakeS3Client | None = None) -> tuple[AsyncBucket, FakeS3Client]:
    bucket, fake = make_bucket(client)
    return AsyncBucket(bucket), fake


async def test_async_bucket_upload_download_and_exists() -> None:
    bucket, fake = make_async_bucket()

    await bucket.upload("b.txt", b"data", content_type="text/plain")
    assert fake.calls[-1] == (
        "put_object",
        {"Bucket": "avatars", "Key": "b.txt", "Body": b"data", "ContentType": "text/plain"},
    )
    assert await bucket.download("a.txt") == b"hello"
    assert await bucket.exists("a.txt") is True
    assert await bucket.exists("missing.txt") is False


async def test_async_bucket_runs_calls_off_the_event_loop_thread() -> None:
    loop_thread = threading.get_ident()
    seen_threads: list[int] = []

    class ThreadRecordingClient(FakeS3Client):
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            seen_threads.append(threading.get_ident())
            return super().head_object(**kwargs)

    bucket, _ = make_async_bucket(ThreadRecordingClient())

    await bucket.exists("a.txt")

    assert seen_threads and seen_threads[0] != loop_thread


async def test_async_bucket_streams_an_object() -> None:
    bucket, _ = make_async_bucket()

    stream = await bucket.open("a.txt")

    assert await stream.read() == b"hello"


async def test_async_bucket_iterates_chunks() -> None:
    fake = FakeS3Client()
    fake.objects["big.bin"] = b"abcdefgh"

    class ChunkedBody:
        def __init__(self, data: bytes) -> None:
            self._stream = io.BytesIO(data)

        def read(self, amount: int | None = None) -> bytes:
            return self._stream.read(amount)

    fake.get_object = lambda **kwargs: {"Body": ChunkedBody(fake.objects[kwargs["Key"]])}  # type: ignore[method-assign]
    bucket, _ = make_async_bucket(fake)

    stream = await bucket.open("big.bin")

    assert [chunk async for chunk in stream.iter_chunks(3)] == [b"abc", b"def", b"gh"]


async def test_async_bucket_list_follows_pages() -> None:
    pages = [
        {"Contents": [{"Key": "a"}, {"Key": "b"}], "IsTruncated": True, "NextContinuationToken": "t1"},
        {"Contents": [{"Key": "c"}], "IsTruncated": False},
    ]
    bucket, _ = make_async_bucket(FakeS3Client(list_pages=pages))

    assert [item.key async for item in bucket.list("")] == ["a", "b", "c"]


async def test_async_bucket_maps_errors_like_the_sync_bucket() -> None:
    bucket, _ = make_async_bucket(FakeS3Client(error=client_error("NoSuchKey")))

    with pytest.raises(ObjectNotFoundError):
        await bucket.download("a.txt")


async def test_async_bucket_delete_copy_and_delete_many() -> None:
    bucket, fake = make_async_bucket()

    await bucket.delete("a.txt")
    await bucket.copy("a.txt", "b.txt")
    await bucket.delete_many(["x", "y"])

    assert [name for name, _ in fake.calls] == ["delete_object", "copy_object", "delete_objects"]


async def test_async_bucket_download_to(tmp_path: Path) -> None:
    bucket, _ = make_async_bucket()
    target = tmp_path / "a.txt"

    await bucket.download_to("a.txt", target)

    assert target.read_bytes() == b"hello"


def test_async_presigned_url_warning_points_at_the_caller() -> None:
    bucket, _ = make_async_bucket()

    with pytest.warns(UserWarning, match="not enforced") as caught:
        bucket.presigned_url("a.txt", expires=60)

    assert caught[0].filename == __file__


def test_async_storage_bucket_is_an_async_bucket_cached_per_name() -> None:
    storage = AsyncStorage(environ={**RUNSITE_VARIABLES, "S3_BUCKET": "uploads"})

    bucket = storage.bucket()

    assert isinstance(bucket, AsyncBucket)
    assert bucket.name == "uploads"
    assert storage.bucket("uploads") is bucket
    assert bucket.public_url("x") == "https://s3.runsite.test/uploads/x"


def fake_registry(environ: dict[str, str]) -> tuple[BucketRegistry, list[FakeS3Client]]:
    created_clients: list[FakeS3Client] = []

    def client_factory(credentials: Any) -> FakeS3Client:
        client = FakeS3Client()
        created_clients.append(client)
        return client

    return BucketRegistry(Environment(environ), client_factory=client_factory), created_clients


def test_registry_shares_one_client_between_sync_and_async_buckets() -> None:
    registry, created_clients = fake_registry({**RUNSITE_VARIABLES, "S3_BUCKET": "uploads"})

    sync_bucket = registry.bucket(None, None)
    async_bucket = registry.async_bucket("uploads", None)

    assert len(created_clients) == 1
    assert async_bucket.sync is sync_bucket
    assert registry.async_bucket(None, None) is async_bucket


def test_registry_close_closes_every_client_and_keeps_buckets_usable() -> None:
    registry, created_clients = fake_registry(RUNSITE_VARIABLES)
    first = registry.bucket("avatars", None)
    registry.bucket("avatars", created_key())

    registry.close()

    assert [client.closed for client in created_clients] == [True, True]
    assert registry.bucket("avatars", None) is first
    assert first.download("a.txt") == b"hello"


def test_storage_close_closes_bucket_clients() -> None:
    storage = Storage(environ=RUNSITE_VARIABLES)
    bucket = storage.bucket("avatars")
    closed: list[bool] = []
    bucket.client.close = lambda: closed.append(True)

    storage.close()

    assert closed == [True]


async def test_async_storage_aclose_closes_bucket_clients() -> None:
    storage = AsyncStorage(environ=RUNSITE_VARIABLES)
    bucket = storage.bucket("avatars")
    closed: list[bool] = []
    bucket.client.close = lambda: closed.append(True)

    await storage.aclose()

    assert closed == [True]
