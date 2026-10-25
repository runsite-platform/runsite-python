import hashlib
import os
import uuid
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import runsite

PLATFORM_READY = bool(os.environ.get("RUNSITE_API_KEY") and os.environ.get("RUNSITE_BASE_URL"))
EMAIL_READY = bool(os.environ.get("EMAIL_API_KEY") and os.environ.get("RUNSITE_TEST_RECIPIENT"))
SERVICE_READY = PLATFORM_READY and bool(os.environ.get("RUNSITE_TEST_SERVICE_ID"))
MULTIPART_PAYLOAD_BYTES = 20 * 1024 * 1024
LISTED_OBJECT_COUNT = 5
LIST_PAGE_SIZE = 2
HTTP_TIMEOUT_SECONDS = 60.0

pytestmark = pytest.mark.integration
needs_platform = pytest.mark.skipif(not PLATFORM_READY, reason="needs RUNSITE_API_KEY and RUNSITE_BASE_URL")
needs_email = pytest.mark.skipif(not EMAIL_READY, reason="needs EMAIL_API_KEY and RUNSITE_TEST_RECIPIENT")
needs_service = pytest.mark.skipif(
    not SERVICE_READY, reason="needs RUNSITE_API_KEY, RUNSITE_BASE_URL and RUNSITE_TEST_SERVICE_ID"
)


@pytest.fixture(scope="module")
def live_bucket() -> Iterator[runsite.Bucket]:
    name = f"sdk-it-{uuid.uuid4().hex[:8]}"
    with runsite.Storage() as storage:
        storage.create_bucket(name)
        try:
            key = storage.create_key(bucket=name, delete=True)
            try:
                yield storage.bucket(name, access_key=key)
            finally:
                storage.delete_key(key.id)
        finally:
            storage.delete_bucket(name, confirm=name)


@needs_platform
def test_small_object_round_trip(live_bucket: runsite.Bucket) -> None:
    live_bucket.upload("hello.txt", b"hi", content_type="text/plain")
    assert live_bucket.exists("hello.txt")
    assert live_bucket.download("hello.txt") == b"hi"
    live_bucket.delete("hello.txt")
    assert not live_bucket.exists("hello.txt")


@needs_platform
def test_multipart_upload_above_threshold(live_bucket: runsite.Bucket, tmp_path: Path) -> None:
    payload = os.urandom(MULTIPART_PAYLOAD_BYTES)
    live_bucket.upload("large.bin", payload)
    target = tmp_path / "large.bin"
    live_bucket.download_to("large.bin", target)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()
    live_bucket.delete("large.bin")


@needs_platform
def test_list_follows_pagination_and_delete_many(live_bucket: runsite.Bucket) -> None:
    keys = [f"paged/item-{index}.txt" for index in range(LISTED_OBJECT_COUNT)]
    for key in keys:
        live_bucket.upload(key, key.encode())

    def limit_page_size(params: dict[str, Any], **_: Any) -> None:
        params["MaxKeys"] = LIST_PAGE_SIZE

    events = live_bucket.client.meta.events
    events.register("provide-client-params.s3.ListObjectsV2", limit_page_size)
    try:
        listed = sorted(item.key for item in live_bucket.list("paged/"))
    finally:
        events.unregister("provide-client-params.s3.ListObjectsV2", limit_page_size)
    assert listed == sorted(keys)

    live_bucket.delete_many(keys)
    assert list(live_bucket.list("paged/")) == []


@needs_platform
def test_presigned_put_and_get(live_bucket: runsite.Bucket) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        upload_url = live_bucket.presigned_url("presigned.txt", method="PUT")
        download_url = live_bucket.presigned_url("presigned.txt")
    put_response = httpx.put(upload_url, content=b"via presigned url", timeout=HTTP_TIMEOUT_SECONDS)
    assert put_response.status_code == 200, put_response.text
    get_response = httpx.get(download_url, timeout=HTTP_TIMEOUT_SECONDS)
    assert get_response.status_code == 200, get_response.text
    assert get_response.content == b"via presigned url"
    live_bucket.delete("presigned.txt")


@needs_platform
def test_copy_within_bucket(live_bucket: runsite.Bucket) -> None:
    live_bucket.upload("original.txt", b"copy me")
    live_bucket.copy("original.txt", "copied.txt")
    assert live_bucket.download("copied.txt") == b"copy me"
    live_bucket.delete_many(["original.txt", "copied.txt"])


@needs_service
def test_secret_set_keeps_flag_and_apply() -> None:
    key = f"SDK_IT_{uuid.uuid4().hex[:8].upper()}"
    with runsite.Secrets(service_id=os.environ["RUNSITE_TEST_SERVICE_ID"]) as service_secrets:
        try:
            service_secrets.set(key, "first value")
            service_secrets.set(key, "second value")
            stored = {variable.key: variable for variable in service_secrets.list()}
            assert stored[key].is_secret
            result = service_secrets.apply()
            assert result.status
        finally:
            service_secrets.delete(key)


@needs_email
def test_send_email_to_self() -> None:
    recipient = os.environ["RUNSITE_TEST_RECIPIENT"]
    sender = os.environ.get("RUNSITE_TEST_SENDER", recipient)
    with runsite.Email() as email_client:
        sent = email_client.send(from_=sender, to=recipient, subject="runsite SDK test", text="ok")
        assert sent.status == "sent"
        record = email_client.get(sent.id)
    assert record.status in {"queued", "sent", "failed", "delivered", "bounced", "complained"}
