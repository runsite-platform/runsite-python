from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

SANDBOX_MODES = frozenset({"sandbox", "sandbox_fallback"})


class SentEmail(BaseModel):
    """Result of `send`.

    A successful response always has `status == "sent"`: the provider accepted the email.
    Rejections arrive as errors (`BadRequestError`, `EmailQuotaExceededError`, `RateLimitError`,
    `UpstreamError`, ...), never as a `SentEmail`. A later "failed", "bounced" or "delivered"
    shows up only through `Email.get` or a webhook.

    `from_` is the effective sender: in sandbox mode it is the shared sandbox address, not the one
    passed to `send`. `message` is a human-readable description of the result, not the email body.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    status: str
    mode: str
    from_: str = Field(alias="from")
    to: list[str]
    message: str | None = None

    @property
    def is_sandbox(self) -> bool:
        """True when the sender domain is not verified and the shared sandbox sender was used."""
        return self.mode in SANDBOX_MODES


class EmailRecord(BaseModel):
    """A stored email as returned by `Email.get`.

    `status` is one of "queued", "sent", "failed", "delivered", "bounced" or "complained".
    `created_at` is timezone-aware.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    to: list[str]
    subject: str
    status: str
    provider: str | None = None
    provider_message_id: str | None = None
    created_at: datetime
