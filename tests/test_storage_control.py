import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from runsite._errors import ConflictError, PermissionDeniedError
from runsite._storage.control import DELETE_BUCKET_TIMEOUT_SECONDS, AsyncStorage, Storage
from runsite._storage.models import BucketInfo
from tests.constants import API_KEY, BASE_URL

BUCKET_ID = "5b0f6d2a-3c1e-4f8a-9d7b-2e6c1a4b8f00"
KEY_ID = "9a1c2b3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"
SECRET_VALUE = "sk-test-value"
BUCKET = {
    "id": BUCKET_ID,
    "name": "avatars",
    "is_public": False,
    "created_at": "2026-10-22T10:00:00Z",
    "storage_bytes_used": 10,
    "object_count": 2,
}
CREATED_KEY = {
    "id": KEY_ID,
    "access_key": "AKIA1",
    "secret_key": SECRET_VALUE,
    "name": None,
    "permissions": {"read": True, "write": True, "delete": True, "admin": False},
    "bucket_id": BUCKET_ID,
    "endpoint_url": "https://s3.runsite.app",
    "region": "eu-central",
}
KEY_INFO = {
    "id": KEY_ID,
    "access_key": "AKIA1",
    "name": "worker",
    "permissions": {"read": True, "write": True, "delete": False},
    "bucket_id": BUCKET_ID,
    "is_active": True,
    "created_at": "2026-10-22T10:00:00Z",
}
KEY_LISTING = {"keys": [KEY_INFO], "total": 1, "page": 1, "size": 50}


@pytest.fixture
def storage() -> Iterator[Storage]:
    client = Storage(api_key=API_KEY, base_url=BASE_URL)
    yield client
    client.close()


def test_create_bucket(api: respx.MockRouter, storage: Storage) -> None:
    route = api.post("/storage/buckets").respond(201, json=BUCKET)

    bucket = storage.create_bucket("avatars", public=False)

    assert json.loads(route.calls.last.request.content) == {"name": "avatars", "is_public": False}
    assert bucket.id == BUCKET_ID


def test_create_bucket_conflict(api: respx.MockRouter, storage: Storage) -> None:
    api.post("/storage/buckets").respond(409, json={"detail": "Bucket name already taken"})

    with pytest.raises(ConflictError):
        storage.create_bucket("avatars")


def test_list_and_get_buckets(api: respx.MockRouter, storage: Storage) -> None:
    api.get("/storage/buckets").respond(200, json={"buckets": [BUCKET], "total": 1})
    api.get("/storage/buckets/avatars").respond(200, json=BUCKET)

    assert [bucket.name for bucket in storage.list_buckets()] == ["avatars"]
    assert storage.get_bucket("avatars").object_count == 2


def test_update_bucket_always_sends_is_public(api: respx.MockRouter, storage: Storage) -> None:
    route = api.patch("/storage/buckets/avatars").respond(200, json={**BUCKET, "is_public": True})

    assert storage.update_bucket("avatars", public=True).is_public is True
    assert json.loads(route.calls.last.request.content) == {"is_public": True}


def test_delete_bucket_requires_matching_confirmation(storage: Storage) -> None:
    with pytest.raises(ValueError, match="confirm"):
        storage.delete_bucket("avatars", confirm="avatar")


def test_delete_bucket_uses_long_timeout(api: respx.MockRouter, storage: Storage) -> None:
    route = api.delete("/storage/buckets/avatars").respond(204)

    storage.delete_bucket("avatars", confirm="avatars")

    assert route.calls.last.request.extensions["timeout"]["read"] == DELETE_BUCKET_TIMEOUT_SECONDS


def test_create_key_resolves_bucket_name(api: respx.MockRouter, storage: Storage) -> None:
    api.get("/storage/buckets/avatars").respond(200, json=BUCKET)
    route = api.post("/storage/keys").respond(201, json=CREATED_KEY)

    key = storage.create_key(bucket="avatars", delete=True, name="worker")

    assert json.loads(route.calls.last.request.content) == {
        "name": "worker",
        "bucket_id": BUCKET_ID,
        "permissions": {"read": True, "write": True, "delete": True},
    }
    assert key.secret_key.reveal() == SECRET_VALUE
    assert SECRET_VALUE not in repr(key)
    assert SECRET_VALUE not in str(key.model_dump())


def test_workspace_wide_key(api: respx.MockRouter, storage: Storage) -> None:
    route = api.post("/storage/keys").respond(201, json={**CREATED_KEY, "bucket_id": None})

    storage.create_key()

    assert "bucket_id" not in json.loads(route.calls.last.request.content)


def test_workspace_wide_key_needs_admin_scope(api: respx.MockRouter, storage: Storage) -> None:
    body = {"detail": {"code": "insufficient_scope", "required": "admin", "actual": "write"}}
    api.post("/storage/keys").respond(403, json=body)

    with pytest.raises(PermissionDeniedError) as caught:
        storage.create_key()
    assert caught.value.required_scope == "admin"


def test_list_and_delete_keys(api: respx.MockRouter, storage: Storage) -> None:
    api.get("/storage/keys").respond(200, json=KEY_LISTING)
    route = api.delete(f"/storage/keys/{KEY_ID}").respond(204)

    keys = storage.list_keys()

    assert [key.id for key in keys] == [KEY_ID]
    assert keys[0].permissions.delete is False
    storage.delete_key(KEY_ID)
    assert route.called


def test_delete_key_rejects_non_uuid(storage: Storage) -> None:
    with pytest.raises(ValueError, match="UUID"):
        storage.delete_key("../buckets/avatars")


async def test_async_control_plane(api: respx.MockRouter) -> None:
    api.post("/storage/buckets").respond(201, json=BUCKET)
    api.get("/storage/buckets").respond(200, json={"buckets": [BUCKET], "total": 1})
    api.get("/storage/buckets/avatars").respond(200, json=BUCKET)
    update_route = api.patch("/storage/buckets/avatars").respond(200, json={**BUCKET, "is_public": True})
    delete_bucket_route = api.delete("/storage/buckets/avatars").respond(204)
    key_route = api.post("/storage/keys").respond(201, json=CREATED_KEY)
    api.get("/storage/keys").respond(200, json=KEY_LISTING)
    delete_key_route = api.delete(f"/storage/keys/{KEY_ID}").respond(204)

    async with AsyncStorage(api_key=API_KEY, base_url=BASE_URL) as client:
        assert (await client.create_bucket("avatars")).name == "avatars"
        assert [bucket.name for bucket in await client.list_buckets()] == ["avatars"]
        assert (await client.get_bucket("avatars")).id == BUCKET_ID
        assert (await client.update_bucket("avatars", public=True)).is_public is True
        key = await client.create_key(bucket="avatars")
        assert [info.id for info in await client.list_keys()] == [KEY_ID]
        await client.delete_key(key.id)
        with pytest.raises(ValueError, match="confirm"):
            await client.delete_bucket("avatars", confirm="other")
        await client.delete_bucket("avatars", confirm="avatars")

    assert json.loads(update_route.calls.last.request.content) == {"is_public": True}
    assert json.loads(key_route.calls.last.request.content)["bucket_id"] == BUCKET_ID
    assert delete_key_route.called
    assert delete_bucket_route.calls.last.request.extensions["timeout"]["read"] == DELETE_BUCKET_TIMEOUT_SECONDS


def test_create_key_for_bucket_info_sends_its_id(api: respx.MockRouter, storage: Storage) -> None:
    route = api.post("/storage/keys").respond(201, json=CREATED_KEY)

    storage.create_key(bucket=BucketInfo.model_validate(BUCKET))

    assert json.loads(route.calls.last.request.content)["bucket_id"] == BUCKET_ID


async def test_async_create_key_for_bucket_info_sends_its_id(api: respx.MockRouter) -> None:
    route = api.post("/storage/keys").respond(201, json=CREATED_KEY)

    async with AsyncStorage(api_key=API_KEY, base_url=BASE_URL) as client:
        await client.create_key(bucket=BucketInfo.model_validate(BUCKET))

    assert json.loads(route.calls.last.request.content)["bucket_id"] == BUCKET_ID


def test_list_keys_sends_page_and_size(api: respx.MockRouter, storage: Storage) -> None:
    route = api.get("/storage/keys").respond(200, json=KEY_LISTING)

    storage.list_keys(page=3, size=10)

    assert dict(route.calls.last.request.url.params) == {"page": "3", "size": "10"}


def test_long_timeout_applies_only_to_delete_bucket(api: respx.MockRouter) -> None:
    listing = api.get("/storage/buckets").respond(200, json={"buckets": [], "total": 0})
    deletion = api.delete("/storage/buckets/avatars").respond(204)
    with Storage(api_key=API_KEY, base_url=BASE_URL, timeout=5.0) as client:
        client.delete_bucket("avatars", confirm="avatars")
        client.list_buckets()

    assert deletion.calls.last.request.extensions["timeout"]["read"] == DELETE_BUCKET_TIMEOUT_SECONDS
    assert listing.calls.last.request.extensions["timeout"]["read"] == 5.0


def test_timeout_above_the_delete_minimum_is_kept(api: respx.MockRouter) -> None:
    route = api.delete("/storage/buckets/avatars").respond(204)
    with Storage(api_key=API_KEY, base_url=BASE_URL, timeout=300.0) as client:
        client.delete_bucket("avatars", confirm="avatars")

    assert route.calls.last.request.extensions["timeout"]["read"] == 300.0


async def test_async_delete_bucket_uses_long_timeout(api: respx.MockRouter) -> None:
    route = api.delete("/storage/buckets/avatars").respond(204)
    async with AsyncStorage(api_key=API_KEY, base_url=BASE_URL) as client:
        await client.delete_bucket("avatars", confirm="avatars")

    assert route.calls.last.request.extensions["timeout"]["read"] == DELETE_BUCKET_TIMEOUT_SECONDS


def test_supplied_http_client_is_used_for_every_request() -> None:
    seen_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(f"{request.method} {request.url.path}")
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"buckets": [], "total": 0})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http_client,
        Storage(api_key=API_KEY, base_url=BASE_URL, http_client=http_client) as client,
    ):
        client.list_buckets()
        client.delete_bucket("avatars", confirm="avatars")

    assert seen_requests == ["GET /api/v1/storage/buckets", "DELETE /api/v1/storage/buckets/avatars"]
