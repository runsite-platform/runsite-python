import hashlib
import hmac
import json

import pytest

from runsite._email import Email
from runsite._email.webhooks import WEBHOOK_EVENTS, verify_webhook
from runsite._errors import WebhookVerificationError

SECRET = "whsec_test"
NOW = 1_800_000_000
BODY = json.dumps(
    {"event": "delivered", "email_id": "e1", "recipient": "c@d.com", "service_id": "s1", "provider": "x"}
).encode()


def sign(body: bytes, timestamp: int | str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def headers(timestamp: int = NOW, signature: str | None = None) -> dict[str, str]:
    return {
        "X-Runsite-Timestamp": str(timestamp),
        "X-Runsite-Signature": signature if signature is not None else sign(BODY, timestamp),
    }


class HeaderPairs:
    """Mimics framework header objects (werkzeug, Starlette) that are not plain dicts."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def items(self) -> list[tuple[str, str]]:
        return list(self._pairs)


def test_valid_webhook() -> None:
    event = verify_webhook(BODY, headers(), secret=SECRET, now=NOW)

    assert (event.event, event.email_id, event.recipient, event.service_id) == ("delivered", "e1", "c@d.com", "s1")
    assert event.data["provider"] == "x"


def test_header_lookup_is_case_insensitive() -> None:
    lowered = {name.lower(): value for name, value in headers().items()}

    assert verify_webhook(BODY, lowered, secret=SECRET, now=NOW).event == "delivered"


def test_framework_header_object_is_accepted() -> None:
    header_object = HeaderPairs(list(headers().items()))

    assert verify_webhook(BODY, header_object, secret=SECRET, now=NOW).event == "delivered"


def test_dual_send_headers_verify() -> None:
    dual = {**headers(), "X-Runway-Timestamp": str(NOW), "X-Runway-Signature": sign(BODY, NOW)}

    assert verify_webhook(BODY, dual, secret=SECRET, now=NOW).event == "delivered"


def test_tampered_body_fails() -> None:
    with pytest.raises(WebhookVerificationError, match="signature"):
        verify_webhook(BODY + b" ", headers(), secret=SECRET, now=NOW)


def test_wrong_secret_fails() -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(BODY, headers(), secret="other", now=NOW)


def test_non_ascii_signature_fails_cleanly() -> None:
    with pytest.raises(WebhookVerificationError, match="signature"):
        verify_webhook(BODY, headers(signature="é"), secret=SECRET, now=NOW)


@pytest.mark.parametrize("skew", [301, -301])
def test_stale_or_future_timestamp_fails(skew: int) -> None:
    with pytest.raises(WebhookVerificationError, match="timestamp"):
        verify_webhook(BODY, headers(timestamp=NOW + skew), secret=SECRET, now=NOW)


@pytest.mark.parametrize("bad_timestamp", ["yesterday", "", "+1800000000", " 1800000000", "1_800_000_000", "١٨٠٠"])
def test_non_digit_timestamp_fails(bad_timestamp: str) -> None:
    bad = {"X-Runsite-Timestamp": bad_timestamp, "X-Runsite-Signature": sign(BODY, bad_timestamp)}

    with pytest.raises(WebhookVerificationError, match="timestamp"):
        verify_webhook(BODY, bad, secret=SECRET, now=NOW)


def test_missing_headers_fail() -> None:
    with pytest.raises(WebhookVerificationError, match="X-Runsite-Signature"):
        verify_webhook(BODY, {"X-Runsite-Timestamp": str(NOW)}, secret=SECRET, now=NOW)


def test_legacy_runway_headers_alone_are_rejected() -> None:
    legacy = {"X-Runway-Timestamp": str(NOW), "X-Runway-Signature": sign(BODY, NOW)}

    with pytest.raises(WebhookVerificationError):
        verify_webhook(BODY, legacy, secret=SECRET, now=NOW)


def test_unknown_event_is_accepted() -> None:
    body = json.dumps({"event": "future_event", "email_id": "e1"}).encode()
    signed = {"X-Runsite-Timestamp": str(NOW), "X-Runsite-Signature": sign(body, NOW)}

    event = verify_webhook(body, signed, secret=SECRET, now=NOW)

    assert event.event == "future_event"
    assert event.event not in WEBHOOK_EVENTS


def test_webhook_events_match_the_service() -> None:
    service_events = {"sent", "delivered", "bounced", "complained", "opened", "clicked", "unsubscribed"}

    assert service_events == WEBHOOK_EVENTS


def test_string_payload_is_accepted() -> None:
    assert verify_webhook(BODY.decode(), headers(), secret=SECRET, now=NOW).event == "delivered"


def test_available_on_email_client() -> None:
    assert Email.webhooks.verify is verify_webhook
