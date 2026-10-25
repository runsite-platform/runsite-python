from collections.abc import Iterator

import httpx
import pytest
import respx

from runsite._dialects import api_error_from_response
from runsite._errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    ConfigurationError,
    NotFoundError,
    ServiceUnavailableError,
)
from runsite._http import Request, RequestSettings, SyncTransport, backoff_delay
from tests.conftest import FakeClock
from tests.constants import API_KEY, API_URL
from tests.helpers import single


def make_settings(max_retries: int = 2) -> RequestSettings:
    return RequestSettings(
        base_url=API_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=5.0,
        max_retries=max_retries,
        error_factory=api_error_from_response,
    )


@pytest.fixture
def transport() -> Iterator[SyncTransport]:
    sync_transport = SyncTransport(make_settings)
    yield sync_transport
    sync_transport.close()


def test_sends_auth_and_user_agent(api: respx.MockRouter, transport: SyncTransport) -> None:
    route = api.get("/users/me").respond(200, json={"ok": True})

    assert transport.run(single(Request("GET", "/users/me"))) == {"ok": True}
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["User-Agent"].startswith("runsite-python/")


def test_drops_none_query_params(api: respx.MockRouter, transport: SyncTransport) -> None:
    route = api.get("/keys").respond(200, json={})

    transport.run(single(Request("GET", "/keys", params={"page": 1, "size": None})))

    assert dict(route.calls.last.request.url.params) == {"page": "1"}


def test_empty_body_returns_none(api: respx.MockRouter, transport: SyncTransport) -> None:
    api.delete("/thing").respond(204)

    assert transport.run(single(Request("DELETE", "/thing"))) is None


def test_non_json_success_body_raises_connection_error(api: respx.MockRouter, transport: SyncTransport) -> None:
    api.get("/thing").respond(200, text="<html>proxy page</html>")

    with pytest.raises(APIConnectionError, match="not valid JSON"):
        transport.run(single(Request("GET", "/thing")))


def test_error_status_uses_error_factory(api: respx.MockRouter, transport: SyncTransport) -> None:
    api.get("/thing").respond(404, json={"detail": "Bucket not found"})

    with pytest.raises(NotFoundError, match="Bucket not found"):
        transport.run(single(Request("GET", "/thing")))


def test_redirect_status_is_an_error(api: respx.MockRouter, transport: SyncTransport) -> None:
    route = api.get("/thing").respond(302, headers={"Location": "https://elsewhere.test/"})

    with pytest.raises(APIStatusError, match=r"^302 Found$") as caught:
        transport.run(single(Request("GET", "/thing")))
    assert type(caught.value) is APIStatusError
    assert route.call_count == 1


@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
def test_get_is_retried_on_retryable_status(
    api: respx.MockRouter, transport: SyncTransport, fake_clock: FakeClock, status_code: int
) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.Response(status_code), httpx.Response(200, json={"ok": True})])

    assert transport.run(single(Request("GET", "/thing"))) == {"ok": True}
    assert route.call_count == 2
    assert fake_clock.sleeps == [0.5]


def test_get_gives_up_after_max_retries(api: respx.MockRouter, transport: SyncTransport, fake_clock: FakeClock) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(503)])

    with pytest.raises(ServiceUnavailableError):
        transport.run(single(Request("GET", "/thing")))
    assert route.call_count == 3
    assert fake_clock.sleeps == [0.5, 1.0]


def test_retry_after_header_sets_delay(api: respx.MockRouter, transport: SyncTransport, fake_clock: FakeClock) -> None:
    api.get("/thing").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "20"}), httpx.Response(200, json={})]
    )

    transport.run(single(Request("GET", "/thing")))

    assert fake_clock.sleeps == [20.0]


def test_backoff_caps() -> None:
    assert backoff_delay(10, None) == 8.0
    assert backoff_delay(0, "600") == 60.0
    assert backoff_delay(0, "-3") == 0.0
    assert backoff_delay(1, "soon") == 1.0
    assert backoff_delay(0, "nan") == 0.5
    assert backoff_delay(0, "inf") == 0.5


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_is_not_retried_on_server_error(
    api: respx.MockRouter, transport: SyncTransport, fake_clock: FakeClock, method: str
) -> None:
    route = api.route(method=method, url="/thing").mock(side_effect=[httpx.Response(503)])

    with pytest.raises(ServiceUnavailableError):
        transport.run(single(Request(method, "/thing", json={})))
    assert route.call_count == 1
    assert fake_clock.sleeps == []


@pytest.mark.parametrize(
    "never_sent_error",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow connect"), httpx.PoolTimeout("pool full")],
)
def test_write_is_retried_when_request_never_left(
    api: respx.MockRouter, transport: SyncTransport, never_sent_error: httpx.TransportError
) -> None:
    route = api.post("/thing").mock(side_effect=[never_sent_error, httpx.Response(201, json={"id": 1})])

    assert transport.run(single(Request("POST", "/thing", json={}))) == {"id": 1}
    assert route.call_count == 2


def test_write_read_timeout_is_not_retried(api: respx.MockRouter, transport: SyncTransport) -> None:
    route = api.post("/thing").mock(side_effect=[httpx.ReadTimeout("slow")])

    with pytest.raises(APITimeoutError, match="may or may not have been applied"):
        transport.run(single(Request("POST", "/thing", json={})))
    assert route.call_count == 1


def test_get_transport_error_is_retried_then_raised(api: respx.MockRouter, transport: SyncTransport) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.ReadError("reset")] * 3)

    with pytest.raises(APIConnectionError, match="did not respond"):
        transport.run(single(Request("GET", "/thing")))
    assert route.call_count == 3


def test_unreachable_host(api: respx.MockRouter, transport: SyncTransport) -> None:
    api.get("/thing").mock(side_effect=[httpx.ConnectError("refused")] * 3)

    with pytest.raises(APIConnectionError, match="Could not reach") as caught:
        transport.run(single(Request("GET", "/thing")))
    assert type(caught.value) is APIConnectionError


@pytest.mark.parametrize("timeout_error", [httpx.ConnectTimeout("slow connect"), httpx.PoolTimeout("pool full")])
def test_connect_timeout_raises_timeout_error(
    api: respx.MockRouter, transport: SyncTransport, timeout_error: httpx.TimeoutException
) -> None:
    route = api.get("/thing").mock(side_effect=[timeout_error] * 3)

    with pytest.raises(APITimeoutError, match="Could not reach"):
        transport.run(single(Request("GET", "/thing")))
    assert route.call_count == 3


@pytest.mark.parametrize(
    "client_error",
    [
        httpx.UnsupportedProtocol("Request URL has an unsupported protocol 'ftp://'"),
        httpx.ProxyError("proxy refused"),
        httpx.LocalProtocolError("illegal header value"),
    ],
)
def test_client_side_transport_error_is_not_retried(
    api: respx.MockRouter, transport: SyncTransport, fake_clock: FakeClock, client_error: httpx.TransportError
) -> None:
    route = api.get("/thing").mock(side_effect=[client_error])

    with pytest.raises(APIConnectionError, match="Request to the Runsite service failed") as caught:
        transport.run(single(Request("GET", "/thing")))
    assert type(caught.value) is APIConnectionError
    assert route.call_count == 1
    assert fake_clock.sleeps == []


def test_remote_protocol_error_is_not_retried(api: respx.MockRouter, transport: SyncTransport) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.RemoteProtocolError("Server disconnected")])

    with pytest.raises(APIConnectionError, match="did not respond"):
        transport.run(single(Request("GET", "/thing")))
    assert route.call_count == 1


def test_redirect_loop_is_wrapped(api: respx.MockRouter, transport: SyncTransport) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.TooManyRedirects("loop")])

    with pytest.raises(APIConnectionError, match="Request to the Runsite service failed"):
        transport.run(single(Request("GET", "/thing")))
    assert route.call_count == 1


def test_invalid_url_is_a_configuration_error(api: respx.MockRouter, transport: SyncTransport) -> None:
    api.get("/thing").mock(side_effect=[httpx.InvalidURL("Invalid non-printable ASCII character in URL")])

    with pytest.raises(ConfigurationError, match="Invalid Runsite service URL"):
        transport.run(single(Request("GET", "/thing")))


def test_max_retries_zero_disables_retries(api: respx.MockRouter) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.Response(503)])
    no_retry_transport = SyncTransport(lambda: make_settings(max_retries=0))

    with pytest.raises(ServiceUnavailableError):
        no_retry_transport.run(single(Request("GET", "/thing")))
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("timeout", "max_retries"), [(0.0, 1), (-1.0, 1), (float("nan"), 1), (float("inf"), 1), (5.0, -1)]
)
def test_invalid_settings_are_rejected(timeout: float, max_retries: int) -> None:
    with pytest.raises(ValueError):
        RequestSettings(
            base_url=API_URL,
            headers={},
            timeout=timeout,
            max_retries=max_retries,
            error_factory=api_error_from_response,
        )


def test_external_http_client_is_left_open() -> None:
    http_client = httpx.Client()
    SyncTransport(make_settings, http_client=http_client).close()

    assert not http_client.is_closed
    http_client.close()
