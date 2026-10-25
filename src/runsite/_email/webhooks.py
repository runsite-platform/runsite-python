from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from runsite._errors import WebhookVerificationError

SIGNATURE_HEADER = "X-Runsite-Signature"
TIMESTAMP_HEADER = "X-Runsite-Timestamp"
DEFAULT_TOLERANCE_SECONDS = 300
WEBHOOK_EVENTS = frozenset({"sent", "delivered", "bounced", "complained", "opened", "clicked", "unsubscribed"})
"""Event names a webhook can subscribe to today. New names may appear; they are never rejected."""


class HeadersLike(Protocol):
    """Anything with `.items()` yielding (name, value) pairs: a dict, werkzeug or Starlette headers."""

    def items(self) -> Iterable[tuple[str, str]]: ...


class WebhookEvent(BaseModel):
    """One verified webhook delivery.

    `recipient` is the first `to` address of the email only; cc, bcc and further `to` addresses are
    not reported. `data` holds the whole JSON body; keys other than the envelope fields (`event`,
    `email_id`, `recipient`, `service_id`) are provider-specific and may change.
    """

    model_config = ConfigDict(extra="allow")

    event: str
    email_id: str | None = None
    recipient: str | None = None
    service_id: str | None = None
    data: dict[str, Any]


def _header(headers: HeadersLike, name: str) -> str:
    lowered = name.lower()
    for header_name, value in headers.items():
        if header_name.lower() == lowered:
            return value
    raise WebhookVerificationError(f"Missing {name} header")


def _signatures_match(expected: str, received: str) -> bool:
    return hmac.compare_digest(expected.encode(), received.encode("utf-8", "surrogateescape"))


def verify_webhook(
    payload: bytes | str,
    headers: HeadersLike,
    *,
    secret: str,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> WebhookEvent:
    """Check a Runsite email webhook and return its parsed event.

    Pass the raw request body exactly as received; re-serialised JSON will not verify. Only the
    `X-Runsite-Signature` and `X-Runsite-Timestamp` headers are read. The `secret` is shown once,
    when the webhook is created in the dashboard, and cannot be rotated; to change it, delete the
    webhook and create a new one.

    Deliveries are at-least-once: a failed delivery is retried, so the same event can arrive more
    than once and there is no delivery id yet. Make the handler idempotent, for example by keying on
    `(email_id, event)`. Unknown event names are accepted, not rejected.
    """
    body = payload.encode() if isinstance(payload, str) else payload
    signature = _header(headers, SIGNATURE_HEADER)
    timestamp_text = _header(headers, TIMESTAMP_HEADER)
    if not (timestamp_text.isascii() and timestamp_text.isdigit()):
        raise WebhookVerificationError("Invalid webhook timestamp")
    timestamp = int(timestamp_text)
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance:
        raise WebhookVerificationError("Webhook timestamp is outside the allowed tolerance")
    expected = hmac.new(secret.encode(), f"{timestamp_text}.".encode() + body, hashlib.sha256).hexdigest()
    if not _signatures_match(expected, signature):
        raise WebhookVerificationError("Invalid webhook signature")
    try:
        data = json.loads(body)
    except ValueError:
        raise WebhookVerificationError("Webhook body is not valid JSON") from None
    if not isinstance(data, dict) or "event" not in data:
        raise WebhookVerificationError("Webhook body has no event")
    return WebhookEvent(
        event=str(data["event"]),
        email_id=data.get("email_id"),
        recipient=data.get("recipient"),
        service_id=data.get("service_id"),
        data=data,
    )


class Webhooks:
    """Namespace exposed as `Email.webhooks` / `runsite.email.webhooks`."""

    verify = staticmethod(verify_webhook)
