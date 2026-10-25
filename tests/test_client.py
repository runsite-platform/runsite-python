import respx

from runsite._client import AsyncClient, SyncClient
from runsite._dialects import api_error_from_response
from runsite._http import Request, RequestSettings
from tests.constants import API_URL
from tests.helpers import single


class CountingSettings:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> RequestSettings:
        self.calls += 1
        return RequestSettings(
            base_url=API_URL, headers={}, timeout=5.0, max_retries=0, error_factory=api_error_from_response
        )


def test_settings_resolve_on_first_request_and_again_after_close(api: respx.MockRouter) -> None:
    api.get("/thing").respond(200, json={"ok": True})
    settings = CountingSettings()
    client = SyncClient(settings, http_client=None)

    assert settings.calls == 0
    client._run(single(Request("GET", "/thing")))
    client._run(single(Request("GET", "/thing")))
    assert settings.calls == 1

    client.close()
    client._run(single(Request("GET", "/thing")))
    assert settings.calls == 2
    client.close()


async def test_async_settings_resolve_on_first_request_and_again_after_close(api: respx.MockRouter) -> None:
    api.get("/thing").respond(200, json={"ok": True})
    settings = CountingSettings()
    client = AsyncClient(settings, http_client=None)

    assert settings.calls == 0
    await client._run(single(Request("GET", "/thing")))
    await client._run(single(Request("GET", "/thing")))
    assert settings.calls == 1

    await client.aclose()
    await client._run(single(Request("GET", "/thing")))
    assert settings.calls == 2
    await client.aclose()
