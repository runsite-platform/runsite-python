from collections.abc import Iterator

import pytest
import respx

from tests.constants import API_URL


class FakeClock:
    """Records sleeps instead of sleeping."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def async_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr("runsite._http._sleep", clock.sleep)
    monkeypatch.setattr("runsite._http._async_sleep", clock.async_sleep)
    return clock


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RUNSITE_API_KEY",
        "RUNSITE_BASE_URL",
        "RUNSITE_SERVICE_ID",
        "EMAIL_API_KEY",
        "EMAIL_API_URL",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_ENDPOINT_URL",
        "S3_BUCKET",
        "S3_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ENDPOINT_URL_S3",
        "APP_NAME",
        "APP_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API_URL, assert_all_called=True) as router:
        yield router
