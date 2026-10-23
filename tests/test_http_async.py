from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from runsite._errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    ConfigurationError,
    ServiceUnavailableError,
)
from runsite._http import AsyncTransport
from tests.conftest import FakeClock
from tests.constants import API_KEY
from tests.test_http_sync import make_settings


@pytest.fixture
async def transport() -> AsyncIterator[AsyncTransport]:
    async_transport = AsyncTransport(make_settings())
    yield async_transport
    await async_transport.aclose()


async def test_get_is_retried_on_gateway_error(
    api: respx.MockRouter, transport: AsyncTransport, fake_clock: FakeClock
) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.Response(502), httpx.Response(200, json={"ok": True})])

    assert await transport.request("GET", "/thing") == {"ok": True}
    assert route.call_count == 2
    assert fake_clock.sleeps == [0.5]


async def test_write_is_not_retried(api: respx.MockRouter, transport: AsyncTransport) -> None:
    route = api.post("/thing").mock(side_effect=[httpx.Response(503)])

    with pytest.raises(ServiceUnavailableError):
        await transport.request("POST", "/thing", json={})
    assert route.call_count == 1


async def test_write_retried_when_never_sent(api: respx.MockRouter, transport: AsyncTransport) -> None:
    route = api.post("/thing").mock(side_effect=[httpx.ConnectError("refused"), httpx.Response(201, json={"id": 1})])

    assert await transport.request("POST", "/thing", json={}) == {"id": 1}
    assert route.call_count == 2


async def test_write_timeout_reports_uncertainty(api: respx.MockRouter, transport: AsyncTransport) -> None:
    api.post("/thing").mock(side_effect=[httpx.ReadTimeout("slow")])

    with pytest.raises(APITimeoutError, match="may or may not have been applied"):
        await transport.request("POST", "/thing", json={})


async def test_get_gives_up_after_retries(api: respx.MockRouter, transport: AsyncTransport) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.ReadError("reset")] * 3)

    with pytest.raises(APIConnectionError):
        await transport.request("GET", "/thing")
    assert route.call_count == 3


async def test_connect_timeout_raises_timeout_error(api: respx.MockRouter, transport: AsyncTransport) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.ConnectTimeout("slow connect")] * 3)

    with pytest.raises(APITimeoutError, match="Could not reach"):
        await transport.request("GET", "/thing")
    assert route.call_count == 3


async def test_client_side_transport_error_is_not_retried(
    api: respx.MockRouter, transport: AsyncTransport, fake_clock: FakeClock
) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.UnsupportedProtocol("unsupported protocol 'ftp://'")])

    with pytest.raises(APIConnectionError, match="Request to the Runsite service failed"):
        await transport.request("GET", "/thing")
    assert route.call_count == 1
    assert fake_clock.sleeps == []


async def test_redirect_status_is_an_error(api: respx.MockRouter, transport: AsyncTransport) -> None:
    api.get("/thing").respond(302, headers={"Location": "https://elsewhere.test/"})

    with pytest.raises(APIStatusError, match=r"^302 Found$"):
        await transport.request("GET", "/thing")


async def test_redirect_loop_is_wrapped(api: respx.MockRouter, transport: AsyncTransport) -> None:
    route = api.get("/thing").mock(side_effect=[httpx.TooManyRedirects("loop")])

    with pytest.raises(APIConnectionError, match="Request to the Runsite service failed"):
        await transport.request("GET", "/thing")
    assert route.call_count == 1


async def test_invalid_url_is_a_configuration_error(api: respx.MockRouter, transport: AsyncTransport) -> None:
    api.get("/thing").mock(side_effect=[httpx.InvalidURL("Invalid non-printable ASCII character in URL")])

    with pytest.raises(ConfigurationError, match="Invalid Runsite service URL"):
        await transport.request("GET", "/thing")


async def test_external_http_client_is_left_open() -> None:
    http_client = httpx.AsyncClient()
    await AsyncTransport(make_settings(), http_client=http_client).aclose()

    assert not http_client.is_closed
    await http_client.aclose()


async def test_sends_auth_and_user_agent(api: respx.MockRouter, transport: AsyncTransport) -> None:
    route = api.get("/users/me").respond(200, json={"ok": True})

    await transport.request("GET", "/users/me")

    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["User-Agent"].startswith("runsite-python/")
