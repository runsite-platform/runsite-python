import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import cast

import httpx
import pytest
import respx
from pydantic import ValidationError

from runsite._email import AsyncEmail, Email, EmailRecord
from runsite._email.client import DEFAULT_EMAIL_TIMEOUT_SECONDS
from runsite._errors import (
    APITimeoutError,
    ConfigurationError,
    EmailQuotaExceededError,
    ServiceUnavailableError,
    UpstreamError,
)
from runsite._http import SyncTransport
from tests.conftest import FakeClock
from tests.constants import EMAIL_API_KEY, EMAIL_URL

EMAIL_ID = "0b6f3c2e-8f0e-4d3c-9a51-5b7c4f1d2e3a"
SENT = {
    "id": EMAIL_ID,
    "status": "sent",
    "mode": "production",
    "from": "Shop <orders@shop.com>",
    "to": ["client@example.com"],
    "message": "Email sent",
}
RECORD = {
    "id": EMAIL_ID,
    "from": "a@b.com",
    "to": ["c@d.com"],
    "subject": "s",
    "status": "delivered",
    "provider": "sweego",
    "provider_message_id": None,
    "created_at": "2026-10-22T10:00:00+00:00",
}


@pytest.fixture
def email_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=EMAIL_URL, assert_all_called=True) as router:
        yield router


@pytest.fixture
def email() -> Iterator[Email]:
    client = Email(api_key=EMAIL_API_KEY, endpoint=EMAIL_URL)
    yield client
    client.close()


def test_send_maps_fields(email_api: respx.MockRouter, email: Email) -> None:
    route = email_api.post("").respond(200, json=SENT)

    sent = email.send(
        from_="Shop <orders@shop.com>",
        to="client@example.com",
        subject="Order #42",
        html="<p>Thanks</p>",
        reply_to="support@shop.com",
        headers={"X-Order": "42"},
    )

    request = route.calls.last.request
    assert request.headers["Api-Key"] == EMAIL_API_KEY
    assert json.loads(request.content) == {
        "from": "Shop <orders@shop.com>",
        "to": ["client@example.com"],
        "subject": "Order #42",
        "message": "<p>Thanks</p>",
        "reply_to": ["support@shop.com"],
        "headers": {"X-Order": "42"},
    }
    assert sent.id == EMAIL_ID
    assert sent.from_ == "Shop <orders@shop.com>"
    assert sent.is_sandbox is False


def test_send_uses_long_default_timeout(email_api: respx.MockRouter, email: Email) -> None:
    route = email_api.post("").respond(200, json=SENT)

    email.send(from_="a@b.com", to="c@d.com", subject="s", text="t")

    assert DEFAULT_EMAIL_TIMEOUT_SECONDS == 60.0
    assert route.calls.last.request.extensions["timeout"]["read"] == DEFAULT_EMAIL_TIMEOUT_SECONDS


def test_sandbox_mode_is_visible(email_api: respx.MockRouter, email: Email) -> None:
    email_api.post("").respond(200, json={**SENT, "mode": "sandbox_fallback"})

    assert email.send(from_="a@b.com", to=["c@d.com"], subject="s", text="t").is_sandbox is True


def test_body_is_required(email: Email) -> None:
    with pytest.raises(ValueError, match="html or text"):
        email.send(from_="a@b.com", to="c@d.com", subject="s")


def test_recipient_limit_is_checked_locally(email: Email) -> None:
    recipients = [f"user{index}@example.com" for index in range(51)]

    with pytest.raises(ValueError, match="50"):
        email.send(from_="a@b.com", to=recipients, subject="s", text="t")


def test_post_is_not_retried_on_503(email_api: respx.MockRouter, email: Email, fake_clock: FakeClock) -> None:
    unavailable = httpx.Response(
        503, json={"error": {"type": "billing_unavailable", "message": "try later"}}, headers={"Retry-After": "30"}
    )
    route = email_api.post("").mock(side_effect=[unavailable])

    with pytest.raises(ServiceUnavailableError) as raised:
        email.send(from_="a@b.com", to="c@d.com", subject="s", text="t")
    assert raised.value.retry_after == 30
    assert route.call_count == 1
    assert fake_clock.sleeps == []


def test_post_is_not_retried_on_502(email_api: respx.MockRouter, email: Email, fake_clock: FakeClock) -> None:
    upstream_failure = httpx.Response(502, json={"error": {"type": "upstream_unavailable", "message": "x"}})
    route = email_api.post("").mock(side_effect=[upstream_failure])

    with pytest.raises(UpstreamError):
        email.send(from_="a@b.com", to="c@d.com", subject="s", text="t")
    assert route.call_count == 1
    assert fake_clock.sleeps == []


def test_post_read_timeout_is_not_retried(email_api: respx.MockRouter, email: Email, fake_clock: FakeClock) -> None:
    route = email_api.post("").mock(side_effect=[httpx.ReadTimeout("slow")])

    with pytest.raises(APITimeoutError):
        email.send(from_="a@b.com", to="c@d.com", subject="s", text="t")
    assert route.call_count == 1
    assert fake_clock.sleeps == []


def test_quota_error(email_api: respx.MockRouter, email: Email) -> None:
    email_api.post("").respond(
        402, json={"error": {"type": "email_quota_exceeded", "message": "used", "quota": {"limit": 3000}}}
    )

    with pytest.raises(EmailQuotaExceededError) as raised:
        email.send(from_="a@b.com", to="c@d.com", subject="s", text="t")
    assert raised.value.quota == {"limit": 3000}


def test_get_status(email_api: respx.MockRouter, email: Email) -> None:
    email_api.get(f"/{EMAIL_ID}").respond(200, json=RECORD)

    record = email.get(EMAIL_ID)

    assert record.status == "delivered"
    assert record.to == ["c@d.com"]
    assert record.created_at == datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad_email_id", ["", "../domains", "not-a-uuid", "e1/../../x"])
def test_get_rejects_non_uuid_id_before_any_request(email: Email, bad_email_id: str) -> None:
    with pytest.raises(ValueError, match="UUID"):
        email.get(bad_email_id)


def test_client_can_be_reused_after_close(email_api: respx.MockRouter) -> None:
    route = email_api.post("").mock(side_effect=[httpx.Response(200, json=SENT), httpx.Response(200, json=SENT)])
    client = Email(api_key=EMAIL_API_KEY, endpoint=EMAIL_URL)

    client.send(from_="a@b.com", to="c@d.com", subject="s", text="t")
    client.close()
    client.send(from_="a@b.com", to="c@d.com", subject="s", text="t")
    client.close()

    assert route.call_count == 2


def test_endpoint_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAIL_API_KEY", EMAIL_API_KEY)
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.runsite.app/emails").respond(200, json=SENT)
        with Email() as client:
            client.send(from_="a@b.com", to="c@d.com", subject="s", text="t")
    assert route.called


def test_missing_key_fails_on_first_send() -> None:
    client = Email()
    with pytest.raises(ConfigurationError, match="EMAIL_API_KEY"):
        client.send(from_="a@b.com", to="c@d.com", subject="s", text="t")


async def test_async_send(email_api: respx.MockRouter) -> None:
    email_api.post("").respond(200, json=SENT)

    async with AsyncEmail(api_key=EMAIL_API_KEY, endpoint=EMAIL_URL) as client:
        sent = await client.send(from_="a@b.com", to="c@d.com", subject="s", html="<b>x</b>")

    assert sent.status == "sent"


async def test_async_get_and_reuse_after_close(email_api: respx.MockRouter) -> None:
    route = email_api.get(f"/{EMAIL_ID}").mock(
        side_effect=[httpx.Response(200, json=RECORD), httpx.Response(200, json=RECORD)]
    )
    client = AsyncEmail(api_key=EMAIL_API_KEY, endpoint=EMAIL_URL)

    assert (await client.get(EMAIL_ID)).status == "delivered"
    await client.aclose()
    assert (await client.get(EMAIL_ID)).status == "delivered"
    await client.aclose()

    assert route.call_count == 2


async def test_async_get_rejects_non_uuid_id() -> None:
    async with AsyncEmail(api_key=EMAIL_API_KEY, endpoint=EMAIL_URL) as client:
        with pytest.raises(ValueError, match="UUID"):
            await client.get("../domains")


def test_send_maps_text_cc_and_bcc(email_api: respx.MockRouter, email: Email) -> None:
    route = email_api.post("").respond(200, json=SENT)

    email.send(from_="a@b.com", to="c@d.com", subject="s", text="plain", cc="cc@d.com", bcc=["bcc@d.com"])

    assert json.loads(route.calls.last.request.content) == {
        "from": "a@b.com",
        "to": ["c@d.com"],
        "subject": "s",
        "text": "plain",
        "cc": ["cc@d.com"],
        "bcc": ["bcc@d.com"],
    }


def test_empty_recipient_list_is_rejected_locally(email: Email) -> None:
    with pytest.raises(ValueError, match="at least one recipient"):
        email.send(from_="a@b.com", to=[], subject="s", text="t")


def test_recipient_limit_counts_cc_and_bcc(email: Email) -> None:
    def addresses(prefix: str, count: int) -> list[str]:
        return [f"{prefix}{index}@example.com" for index in range(count)]

    with pytest.raises(ValueError, match="50"):
        email.send(
            from_="a@b.com",
            to=addresses("to", 30),
            cc=addresses("cc", 15),
            bcc=addresses("bcc", 6),
            subject="s",
            text="t",
        )


def test_get_rejects_non_string_id(email: Email) -> None:
    with pytest.raises(TypeError, match="string"):
        email.get(cast(str, uuid.UUID(EMAIL_ID)))


def test_endpoint_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAIL_API_KEY", EMAIL_API_KEY)
    monkeypatch.setenv("EMAIL_API_URL", "https://mail.runsite.test/emails")
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://mail.runsite.test/emails").respond(200, json=SENT)
        with Email() as client:
            client.send(from_="a@b.com", to="c@d.com", subject="s", text="t")
    assert route.called


def test_supplied_http_client_is_used() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=SENT)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        Email(api_key=EMAIL_API_KEY, endpoint=EMAIL_URL, http_client=http_client).send(
            from_="a@b.com", to="c@d.com", subject="s", text="t"
        )

    assert seen_urls == [EMAIL_URL]


async def test_async_supplied_http_client_is_used() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=SENT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await AsyncEmail(api_key=EMAIL_API_KEY, endpoint=EMAIL_URL, http_client=http_client).send(
            from_="a@b.com", to="c@d.com", subject="s", text="t"
        )

    assert seen_urls == [EMAIL_URL]


def test_context_manager_closes_the_connection_pool(
    email_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    email_api.post("").respond(200, json=SENT)
    closed_transports: list[object] = []
    original_close = SyncTransport.close

    def recording_close(transport: SyncTransport) -> None:
        closed_transports.append(transport)
        original_close(transport)

    monkeypatch.setattr(SyncTransport, "close", recording_close)
    with Email(api_key=EMAIL_API_KEY, endpoint=EMAIL_URL) as client:
        client.send(from_="a@b.com", to="c@d.com", subject="s", text="t")

    assert len(closed_transports) == 1


async def test_async_send_posts_to_the_endpoint_itself(email_api: respx.MockRouter) -> None:
    route = email_api.post("").respond(200, json=SENT)

    async with AsyncEmail(api_key=EMAIL_API_KEY, endpoint=EMAIL_URL) as client:
        await client.send(from_="a@b.com", to="c@d.com", subject="s", text="t")

    assert str(route.calls.last.request.url) == EMAIL_URL


def test_email_record_requires_status() -> None:
    record_without_status = {name: value for name, value in RECORD.items() if name != "status"}

    with pytest.raises(ValidationError, match="status"):
        EmailRecord.model_validate(record_without_status)
