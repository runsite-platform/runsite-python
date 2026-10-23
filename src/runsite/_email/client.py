from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any, TypeAlias

import httpx

from runsite._config import DEFAULT_EMAIL_URL, read_env, resolve
from runsite._email.models import EmailRecord, SentEmail
from runsite._email.webhooks import Webhooks
from runsite._errors import email_error_from_response
from runsite._http import AsyncTransport, RequestSettings, SyncTransport

MAX_RECIPIENTS = 50
DEFAULT_EMAIL_TIMEOUT_SECONDS = 60.0
EMAIL_KEY_HINT = "Connect an email service to this web service in the Runsite dashboard, or pass api_key=..."
Recipients: TypeAlias = "str | Sequence[str]"


def as_list(recipients: Recipients | None) -> list[str]:
    if recipients is None:
        return []
    return [recipients] if isinstance(recipients, str) else list(recipients)


def build_send_body(
    *,
    from_: str,
    to: Recipients,
    subject: str,
    html: str | None,
    text: str | None,
    cc: Recipients | None,
    bcc: Recipients | None,
    reply_to: Recipients | None,
    headers: Mapping[str, str] | None,
) -> dict[str, Any]:
    if html is None and text is None:
        raise ValueError("Pass html or text (or both)")
    to_list, cc_list, bcc_list = as_list(to), as_list(cc), as_list(bcc)
    if not to_list:
        raise ValueError("'to' must contain at least one recipient")
    if len(to_list) + len(cc_list) + len(bcc_list) > MAX_RECIPIENTS:
        raise ValueError(f"At most {MAX_RECIPIENTS} recipients across to, cc and bcc")
    body: dict[str, Any] = {"from": from_, "to": to_list, "subject": subject}
    optional = {
        "message": html,
        "text": text,
        "cc": cc_list or None,
        "bcc": bcc_list or None,
        "reply_to": as_list(reply_to) or None,
        "headers": dict(headers) if headers else None,
    }
    body.update({name: value for name, value in optional.items() if value is not None})
    return body


def email_path(email_id: str) -> str:
    """Path for one email; the id must be a UUID so it cannot change the URL."""
    if not isinstance(email_id, str):
        raise TypeError("email_id must be a string")
    try:
        normalized_id = uuid.UUID(email_id)
    except ValueError:
        raise ValueError(f"email_id must be a UUID, got {email_id!r}") from None
    return f"/{normalized_id}"


def email_settings(*, api_key: str | None, endpoint: str | None, timeout: float, max_retries: int) -> RequestSettings:
    key = resolve(api_key, "EMAIL_API_KEY", hint=EMAIL_KEY_HINT)
    url = endpoint or read_env("EMAIL_API_URL") or DEFAULT_EMAIL_URL
    return RequestSettings(
        base_url=url,
        headers={"Api-Key": key},
        timeout=timeout,
        max_retries=max_retries,
        error_factory=email_error_from_response,
    )


class Email:
    """Sends transactional email through Runsite.

    The API key comes from `EMAIL_API_KEY`, which Runsite sets when an email service is connected
    to the web service. The SDK does not check the key prefix.

    Sending is never retried after the request left, so an email is not sent twice. One send can
    take a while on the server (billing check, provider call), so the default timeout is 60 seconds.
    `APITimeoutError` from `send` means the email may or may not have been sent: check with `get`
    or a webhook before sending again.

    Errors from `send`:
    - `BadRequestError` (400): invalid input, or in sandbox mode a recipient that is not verified.
    - `EmailQuotaExceededError` (402): the quota is used up; `quota` has the details.
    - `RateLimitError` (429): the daily sending limit is reached. There is no `Retry-After`; the
      service also answers 429 while its rate limiter is unavailable.
    - `UpstreamError` (502) and `ServiceUnavailableError` (503, with `retry_after`).

    Sandbox mode (sender domain not verified): the email goes out from the shared sandbox address,
    `reply_to` defaults to the `from_` you passed, and every recipient must be verified first.
    """

    webhooks = Webhooks()

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        timeout: float = DEFAULT_EMAIL_TIMEOUT_SECONDS,
        max_retries: int = 2,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout = timeout
        self._max_retries = max_retries
        self._http_client = http_client
        self._transport: SyncTransport | None = None

    def _client(self) -> SyncTransport:
        if self._transport is None:
            settings = email_settings(
                api_key=self._api_key, endpoint=self._endpoint, timeout=self._timeout, max_retries=self._max_retries
            )
            self._transport = SyncTransport(settings, http_client=self._http_client)
        return self._transport

    def send(
        self,
        *,
        from_: str,
        to: Recipients,
        subject: str,
        html: str | None = None,
        text: str | None = None,
        cc: Recipients | None = None,
        bcc: Recipients | None = None,
        reply_to: Recipients | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> SentEmail:
        """Send one email. Addresses may be "user@example.com" or "Name <user@example.com>".

        Returns once the provider accepted the email (`status == "sent"`). Raises `APITimeoutError`
        when no answer came in time; the email may have been sent anyway.
        """
        body = build_send_body(
            from_=from_,
            to=to,
            subject=subject,
            html=html,
            text=text,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            headers=headers,
        )
        return SentEmail.model_validate(self._client().request("POST", "", json=body))

    def get(self, email_id: str) -> EmailRecord:
        """Fetch the delivery status of a sent email. Raises `ValueError` if `email_id` is not a UUID."""
        return EmailRecord.model_validate(self._client().request("GET", email_path(email_id)))

    def close(self) -> None:
        """Close the connection pool. The client can still be used; it opens a new pool."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def __enter__(self) -> Email:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()


class AsyncEmail:
    """Async twin of `Email`, with the same timeouts, retry rules and errors."""

    webhooks = Webhooks()

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        timeout: float = DEFAULT_EMAIL_TIMEOUT_SECONDS,
        max_retries: int = 2,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout = timeout
        self._max_retries = max_retries
        self._http_client = http_client
        self._transport: AsyncTransport | None = None

    def _client(self) -> AsyncTransport:
        if self._transport is None:
            settings = email_settings(
                api_key=self._api_key, endpoint=self._endpoint, timeout=self._timeout, max_retries=self._max_retries
            )
            self._transport = AsyncTransport(settings, http_client=self._http_client)
        return self._transport

    async def send(
        self,
        *,
        from_: str,
        to: Recipients,
        subject: str,
        html: str | None = None,
        text: str | None = None,
        cc: Recipients | None = None,
        bcc: Recipients | None = None,
        reply_to: Recipients | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> SentEmail:
        """Send one email; see `Email.send`."""
        body = build_send_body(
            from_=from_,
            to=to,
            subject=subject,
            html=html,
            text=text,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            headers=headers,
        )
        return SentEmail.model_validate(await self._client().request("POST", "", json=body))

    async def get(self, email_id: str) -> EmailRecord:
        """Fetch the delivery status of a sent email; see `Email.get`."""
        return EmailRecord.model_validate(await self._client().request("GET", email_path(email_id)))

    async def aclose(self) -> None:
        """Close the connection pool. The client can still be used; it opens a new pool."""
        if self._transport is not None:
            await self._transport.aclose()
            self._transport = None

    async def __aenter__(self) -> AsyncEmail:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()
