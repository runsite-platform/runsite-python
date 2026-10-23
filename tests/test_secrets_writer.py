import json
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
import respx

from runsite._errors import ConfigurationError, ConflictError, NotFoundError, PermissionDeniedError
from runsite._http import AsyncTransport, SyncTransport
from runsite._secrets import ApplyResult, AsyncSecrets, BulkResult, Secrets
from tests.constants import API_KEY, BASE_URL, SERVICE_ID

ENV_PATH = f"/web-services/{SERVICE_ID}/env"
BULK_PATH = f"{ENV_PATH}/bulk"
APPLY_PATH = f"/web-services/{SERVICE_ID}/apply-env"
APPLYING_BODY = {
    "status": "restarting",
    "container_id": None,
    "host_port": None,
    "message": "Applying environment variables...",
}
SAVED_FOR_NEXT_START_BODY = {
    "status": "stopped",
    "container_id": None,
    "host_port": None,
    "message": "Environment saved. It will be applied the next time the service starts.",
}


def variable(key: str, variable_id: str, is_secret: bool = True) -> dict[str, object]:
    return {
        "id": variable_id,
        "web_service_id": SERVICE_ID,
        "key": key,
        "value": "********",
        "is_secret": is_secret,
        "created_at": "2026-10-22T10:00:00Z",
        "updated_at": "2026-10-22T10:00:00Z",
    }


def listing(*variables: dict[str, object]) -> dict[str, object]:
    return {"environment_variables": list(variables), "total": len(variables)}


def bulk_result(created: int = 0, updated: int = 0, deleted: int = 0) -> dict[str, object]:
    return {"environment_variables": [], "total": 0, "created": created, "updated": updated, "deleted": deleted}


def sent_variables(route: respx.Route, call_index: int = -1) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], json.loads(route.calls[call_index].request.content)["variables"])


@pytest.fixture
def secrets() -> Iterator[Secrets]:
    client = Secrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID)
    yield client
    client.close()


def test_set_many_keeps_existing_flags_and_creates_secret_by_default(api: respx.MockRouter, secrets: Secrets) -> None:
    api.get(ENV_PATH).respond(200, json=listing(variable("LOG_LEVEL", "id-1", is_secret=False)))
    bulk = api.put(BULK_PATH).respond(200, json=bulk_result(created=1, updated=1))

    result = secrets.set_many({"LOG_LEVEL": "debug", "FRESH": "value"})

    assert sent_variables(bulk) == [
        {"id": "id-1", "key": "LOG_LEVEL", "value": "debug", "is_secret": False},
        {"key": "FRESH", "value": "value", "is_secret": True},
    ]
    assert (result.created, result.updated) == (1, 1)


def test_explicit_secret_flag_overrides_existing_flag(api: respx.MockRouter, secrets: Secrets) -> None:
    api.get(ENV_PATH).respond(200, json=listing(variable("LOG_LEVEL", "id-1", is_secret=True)))
    bulk = api.put(BULK_PATH).respond(200, json=bulk_result(updated=1))

    secrets.set("LOG_LEVEL", "info", secret=False)

    assert sent_variables(bulk) == [{"id": "id-1", "key": "LOG_LEVEL", "value": "info", "is_secret": False}]


def test_explicit_secret_flag_on_create(api: respx.MockRouter, secrets: Secrets) -> None:
    api.get(ENV_PATH).respond(200, json=listing())
    bulk = api.put(BULK_PATH).respond(200, json=bulk_result(created=1))

    secrets.set("LOG_LEVEL", "info", secret=False)

    assert sent_variables(bulk) == [{"key": "LOG_LEVEL", "value": "info", "is_secret": False}]


def test_set_many_with_no_values_sends_nothing(secrets: Secrets) -> None:
    with respx.mock(assert_all_called=False) as router:
        assert secrets.set_many({}) == BulkResult(created=0, updated=0, deleted=0)
        assert router.calls.call_count == 0


def test_conflict_between_get_and_put_is_retried_once(api: respx.MockRouter, secrets: Secrets) -> None:
    listings = api.get(ENV_PATH).mock(
        side_effect=[
            httpx.Response(200, json=listing()),
            httpx.Response(200, json=listing(variable("TOKEN", "id-7", is_secret=False))),
        ]
    )
    bulk = api.put(BULK_PATH).mock(
        side_effect=[
            httpx.Response(409, json={"detail": "Environment variable 'TOKEN' already exists"}),
            httpx.Response(200, json=bulk_result(updated=1)),
        ]
    )

    result = secrets.set("TOKEN", "value")

    assert result.updated == 1
    assert (listings.call_count, bulk.call_count) == (2, 2)
    assert sent_variables(bulk, 0) == [{"key": "TOKEN", "value": "value", "is_secret": True}]
    assert sent_variables(bulk, 1) == [{"id": "id-7", "key": "TOKEN", "value": "value", "is_secret": False}]


def test_second_conflict_is_raised(api: respx.MockRouter, secrets: Secrets) -> None:
    api.get(ENV_PATH).mock(side_effect=[httpx.Response(200, json=listing()), httpx.Response(200, json=listing())])
    conflict = {"detail": "Environment variable 'TOKEN' already exists"}
    bulk = api.put(BULK_PATH).mock(side_effect=[httpx.Response(409, json=conflict), httpx.Response(409, json=conflict)])

    with pytest.raises(ConflictError):
        secrets.set("TOKEN", "value")
    assert bulk.call_count == 2


@pytest.mark.parametrize(
    "bad_key", ["lower", "1ABC", "WITH-DASH", "TRAILING\n", "APP_PORT", "APP_NAME", "RUNSITE_SERVICE_ID"]
)
def test_invalid_or_reserved_keys_are_rejected_before_any_request(secrets: Secrets, bad_key: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(ValueError, match=bad_key.strip()):
            secrets.set(bad_key, "value")
        assert router.calls.call_count == 0


def test_key_longer_than_255_characters_is_rejected(secrets: Secrets) -> None:
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(ValueError, match="not a valid key"):
            secrets.set("A" * 256, "value")
        assert router.calls.call_count == 0


@pytest.mark.parametrize("bad_port", ["0", "65536", "http"])
def test_port_must_be_a_valid_port_number(secrets: Secrets, bad_port: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(ValueError, match="PORT must be an integer between 1 and 65535"):
            secrets.set("PORT", bad_port)
        assert router.calls.call_count == 0


def test_non_string_value_raises_type_error_without_echoing_it(secrets: Secrets) -> None:
    with pytest.raises(TypeError) as caught:
        secrets.set("TOKEN", cast(Any, b"raw-bytes-value"))
    assert "TOKEN" in str(caught.value)
    assert "raw-bytes-value" not in str(caught.value)


@pytest.mark.parametrize("non_string_value", [8080, 1.5, True, None])
def test_non_string_scalar_values_are_rejected(secrets: Secrets, non_string_value: object) -> None:
    with pytest.raises(TypeError):
        secrets.set("TOKEN", cast(Any, non_string_value))


def test_empty_value_is_rejected(secrets: Secrets) -> None:
    with pytest.raises(ValueError, match="empty"):
        secrets.set("TOKEN", "")


def test_delete_ignores_missing_keys(api: respx.MockRouter, secrets: Secrets) -> None:
    api.get(ENV_PATH).respond(200, json=listing(variable("OLD", "id-9")))
    bulk = api.put(BULK_PATH).respond(200, json=bulk_result(deleted=1))

    result = secrets.delete("OLD", "NEVER_EXISTED")

    assert sent_variables(bulk) == [{"id": "id-9", "key": "OLD", "deleted": True}]
    assert result.deleted == 1


def test_delete_of_only_missing_keys_sends_nothing(api: respx.MockRouter, secrets: Secrets) -> None:
    api.get(ENV_PATH).respond(200, json=listing())

    assert secrets.delete("NEVER_EXISTED").deleted == 0


def test_list_returns_keys_without_values(api: respx.MockRouter, secrets: Secrets) -> None:
    api.get(ENV_PATH).respond(200, json=listing(variable("FIRST", "id-1", is_secret=False)))

    variables = secrets.list()

    assert [(item.key, item.is_secret, item.web_service_id) for item in variables] == [("FIRST", False, SERVICE_ID)]


def test_apply_queues_a_restart_through_apply_env(api: respx.MockRouter, secrets: Secrets) -> None:
    route = api.post(APPLY_PATH).respond(200, json=APPLYING_BODY)

    result = secrets.apply()

    assert route.call_count == 1
    assert (result.status, result.restarting) == ("restarting", True)
    assert result.message == "Applying environment variables..."


def test_apply_on_stopped_service_saves_for_next_start(api: respx.MockRouter, secrets: Secrets) -> None:
    api.post(APPLY_PATH).respond(200, json=SAVED_FOR_NEXT_START_BODY)

    result = secrets.apply()

    assert (result.status, result.restarting) == ("stopped", False)


def test_apply_conflict_is_raised_and_not_repeated(api: respx.MockRouter, secrets: Secrets) -> None:
    route = api.post(APPLY_PATH).respond(409, json={"detail": "Web service is already deploying. Please wait."})

    with pytest.raises(ConflictError):
        secrets.apply()
    assert route.call_count == 1


def test_read_only_key_is_denied_on_write(api: respx.MockRouter, secrets: Secrets) -> None:
    api.get(ENV_PATH).respond(200, json=listing())
    api.put(BULK_PATH).respond(
        403, json={"detail": {"code": "insufficient_scope", "required": "write", "actual": "read"}}
    )

    with pytest.raises(PermissionDeniedError) as caught:
        secrets.set("TOKEN", "value")
    assert (caught.value.required_scope, caught.value.actual_scope) == ("write", "read")


def test_workspace_role_without_admin_is_denied(api: respx.MockRouter, secrets: Secrets) -> None:
    api.post(APPLY_PATH).respond(403, json={"detail": "Insufficient permissions for this workspace"})

    with pytest.raises(PermissionDeniedError) as caught:
        secrets.apply()
    assert caught.value.required_scope is None


def test_unknown_service_id_is_not_found(api: respx.MockRouter, secrets: Secrets) -> None:
    api.get(ENV_PATH).respond(404, json={"detail": "Web service not found"})

    with pytest.raises(NotFoundError):
        secrets.list()


def test_service_id_from_process_environment(api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNSITE_SERVICE_ID", SERVICE_ID)
    api.get(ENV_PATH).respond(200, json=listing())
    with Secrets(api_key=API_KEY, base_url=BASE_URL) as client:
        assert client.list() == []


def test_settings_come_from_the_injected_mapping(api: respx.MockRouter) -> None:
    route = api.get(ENV_PATH).respond(200, json=listing())
    environ = {"RUNSITE_API_KEY": API_KEY, "RUNSITE_BASE_URL": BASE_URL, "RUNSITE_SERVICE_ID": SERVICE_ID}

    with Secrets(environ=environ) as client:
        assert client.list() == []

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"


def test_injected_mapping_does_not_fall_back_to_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNSITE_API_KEY", API_KEY)
    monkeypatch.setenv("RUNSITE_SERVICE_ID", SERVICE_ID)

    with (
        Secrets(environ={"RUNSITE_SERVICE_ID": SERVICE_ID}) as client,
        pytest.raises(ConfigurationError, match="RUNSITE_API_KEY"),
    ):
        client.list()


def test_missing_service_id_is_a_configuration_error() -> None:
    with (
        Secrets(api_key=API_KEY, base_url=BASE_URL, environ={}) as client,
        pytest.raises(ConfigurationError, match="RUNSITE_SERVICE_ID"),
    ):
        client.list()


def test_missing_api_key_only_fails_on_write() -> None:
    client = Secrets(service_id=SERVICE_ID, environ={"TOKEN": "x"})

    assert client.get("TOKEN") == "x"
    with pytest.raises(ConfigurationError, match="RUNSITE_API_KEY"):
        client.list()


async def test_async_set_many_keeps_existing_flag(api: respx.MockRouter) -> None:
    api.get(ENV_PATH).respond(200, json=listing(variable("LOG_LEVEL", "id-1", is_secret=False)))
    bulk = api.put(BULK_PATH).respond(200, json=bulk_result(updated=1))

    async with AsyncSecrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID) as client:
        result = await client.set_many({"LOG_LEVEL": "debug"})

    assert result.updated == 1
    assert sent_variables(bulk) == [{"id": "id-1", "key": "LOG_LEVEL", "value": "debug", "is_secret": False}]


async def test_async_conflict_is_retried_once(api: respx.MockRouter) -> None:
    api.get(ENV_PATH).mock(
        side_effect=[
            httpx.Response(200, json=listing()),
            httpx.Response(200, json=listing(variable("TOKEN", "id-7"))),
        ]
    )
    bulk = api.put(BULK_PATH).mock(
        side_effect=[
            httpx.Response(409, json={"detail": "Environment variable 'TOKEN' already exists"}),
            httpx.Response(200, json=bulk_result(updated=1)),
        ]
    )

    async with AsyncSecrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID) as client:
        assert (await client.set("TOKEN", "value")).updated == 1

    assert bulk.call_count == 2


async def test_async_set_many_with_no_values_sends_nothing() -> None:
    with respx.mock(assert_all_called=False) as router:
        async with AsyncSecrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID) as client:
            assert (await client.set_many({})).created == 0
        assert router.calls.call_count == 0


async def test_async_list_apply_and_delete(api: respx.MockRouter) -> None:
    api.get(ENV_PATH).respond(200, json=listing(variable("FIRST", "id-1")))
    api.put(BULK_PATH).respond(200, json=bulk_result(deleted=1))
    api.post(APPLY_PATH).respond(200, json=APPLYING_BODY)

    async with AsyncSecrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID) as client:
        assert [item.key for item in await client.list()] == ["FIRST"]
        assert (await client.delete("FIRST")).deleted == 1
        assert (await client.apply()).restarting is True


def test_external_http_client_is_used(api: respx.MockRouter) -> None:
    api.get(ENV_PATH).respond(200, json=listing())
    http_client = httpx.Client()

    with Secrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID, http_client=http_client) as client:
        client.list()

    assert not http_client.is_closed
    http_client.close()


OTHER_SERVICE_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


def test_explicit_arguments_win_over_the_mapping(api: respx.MockRouter) -> None:
    route = api.get(ENV_PATH).respond(200, json=listing())
    environ = {"RUNSITE_API_KEY": "ak_live_from_mapping", "RUNSITE_SERVICE_ID": OTHER_SERVICE_ID}

    with Secrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID, environ=environ) as client:
        client.list()

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"


def test_set_writes_to_the_given_service(api: respx.MockRouter, secrets: Secrets) -> None:
    other_env_path = f"/web-services/{OTHER_SERVICE_ID}/env"
    api.get(other_env_path).respond(200, json=listing())
    bulk = api.put(f"{other_env_path}/bulk").respond(200, json=bulk_result(created=1))

    assert secrets.set("TOKEN", "value", service_id=OTHER_SERVICE_ID).created == 1
    assert sent_variables(bulk) == [{"key": "TOKEN", "value": "value", "is_secret": True}]


def test_supplied_http_client_carries_the_requests() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json=listing())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http_client,
        Secrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID, http_client=http_client) as client,
    ):
        client.list()

    assert seen_paths == [f"/api/v1{ENV_PATH}"]


def test_close_closes_the_connection_pool(api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    api.get(ENV_PATH).respond(200, json=listing())
    closed_transports: list[SyncTransport] = []
    original_close = SyncTransport.close

    def recording_close(transport: SyncTransport) -> None:
        closed_transports.append(transport)
        original_close(transport)

    monkeypatch.setattr(SyncTransport, "close", recording_close)
    with Secrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID) as client:
        client.list()

    assert len(closed_transports) == 1


async def test_async_close_closes_the_connection_pool(api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    api.get(ENV_PATH).respond(200, json=listing())
    closed_transports: list[AsyncTransport] = []
    original_aclose = AsyncTransport.aclose

    async def recording_aclose(transport: AsyncTransport) -> None:
        closed_transports.append(transport)
        await original_aclose(transport)

    monkeypatch.setattr(AsyncTransport, "aclose", recording_aclose)
    async with AsyncSecrets(api_key=API_KEY, base_url=BASE_URL, service_id=SERVICE_ID) as client:
        await client.list()

    assert len(closed_transports) == 1


@pytest.mark.parametrize("status", ["stopped", "sleeping", "failed", "running"])
def test_only_restarting_status_counts_as_restarting(status: str) -> None:
    assert ApplyResult(status=status).restarting is False
    assert ApplyResult(status="restarting").restarting is True
