from datetime import datetime, timezone

import httpx
import pytest

from runsite._dialects import api_error_from_response, email_error_from_response
from runsite._errors import (
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    EmailQuotaExceededError,
    InternalServerError,
    MissingSecretError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
    UnprocessableEntityError,
    UpstreamError,
    parse_retry_after,
)


def make_response(status_code: int, body: object = None, headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://api.runsite.test/anything")
    if body is None:
        return httpx.Response(status_code, headers=headers, request=request)
    return httpx.Response(status_code, json=body, headers=headers, request=request)


def make_text_response(status_code: int, text: str) -> httpx.Response:
    request = httpx.Request("GET", "https://api.runsite.test/anything")
    return httpx.Response(status_code, text=text, request=request)


@pytest.mark.parametrize(
    ("status_code", "error_class"),
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, UnprocessableEntityError),
        (429, RateLimitError),
        (500, InternalServerError),
        (502, UpstreamError),
        (503, ServiceUnavailableError),
        (504, InternalServerError),
        (418, APIStatusError),
        (302, APIStatusError),
    ],
)
def test_api_status_maps_to_error_class(status_code: int, error_class: type[APIStatusError]) -> None:
    error = api_error_from_response(make_response(status_code, {"detail": "nope"}))

    assert type(error) is error_class
    assert error.status_code == status_code
    assert error.detail == "nope"
    assert str(error) == f"{status_code} nope"


def test_scope_detail_is_exposed() -> None:
    body = {"detail": {"code": "insufficient_scope", "required": "write", "actual": "read"}}

    error = api_error_from_response(make_response(403, body))

    assert isinstance(error, PermissionDeniedError)
    assert (error.required_scope, error.actual_scope, error.code) == ("write", "read", "insufficient_scope")
    assert str(error) == "403 API key scope 'read' is below the required 'write'"


def test_scope_properties_are_none_without_a_coded_detail() -> None:
    error = api_error_from_response(make_response(403, {"detail": "Workspace role does not allow this action"}))

    assert isinstance(error, PermissionDeniedError)
    assert (error.required_scope, error.actual_scope, error.code) == (None, None, None)


def test_other_coded_detail_uses_code_and_reason() -> None:
    error = api_error_from_response(make_response(403, {"detail": {"code": "user_blocked", "reason": "abuse"}}))

    assert error.code == "user_blocked"
    assert str(error) == "403 user_blocked: abuse"


def test_validation_errors_are_listed() -> None:
    body = {"detail": [{"loc": ["body", "key"], "msg": "String should match pattern", "type": "string_pattern"}]}

    error = api_error_from_response(make_response(422, body))

    assert isinstance(error, UnprocessableEntityError)
    assert error.errors == [("key", "String should match pattern")]


def test_retry_after_is_parsed_on_any_status_error() -> None:
    error = api_error_from_response(make_response(429, {"detail": "slow"}, headers={"Retry-After": "7"}))

    assert error.retry_after == 7.0


def test_bad_retry_after_is_none() -> None:
    error = api_error_from_response(make_response(429, {"detail": "slow"}, headers={"Retry-After": "soon"}))

    assert error.retry_after is None


@pytest.mark.parametrize("header", ["nan", "inf", "-inf", "soon", "", "Funday, 99 Nob 2026 25:61:00 GMT"])
def test_unusable_retry_after_parses_to_none(header: str) -> None:
    assert parse_retry_after(header) is None


def test_retry_after_http_date_is_seconds_from_now() -> None:
    now = datetime(2026, 10, 21, 7, 27, 30, tzinfo=timezone.utc)

    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=now) == 30.0


def test_retry_after_http_date_without_zone_is_utc() -> None:
    now = datetime(2026, 10, 21, 7, 27, 30, tzinfo=timezone.utc)

    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 -0000", now=now) == 30.0


@pytest.mark.parametrize("header", ["-5", "Thu, 01 Jan 2015 00:00:00 GMT"])
def test_retry_after_property_is_never_negative(header: str) -> None:
    error = api_error_from_response(make_response(429, {"detail": "slow"}, headers={"Retry-After": header}))

    assert error.retry_after == 0.0


def test_non_json_body_falls_back_to_reason_phrase() -> None:
    error = api_error_from_response(make_response(502))

    assert isinstance(error, UpstreamError)
    assert str(error) == "502 Bad Gateway"
    assert error.detail is None


def test_plain_text_api_error_body_becomes_the_detail() -> None:
    rejection = "Failed to deserialize the JSON body into the target type: missing field `is_public`"

    error = api_error_from_response(make_text_response(422, f"{rejection}\n"))

    assert isinstance(error, UnprocessableEntityError)
    assert error.detail == rejection
    assert str(error) == f"422 {rejection}"
    assert error.errors == []


def test_long_plain_text_body_is_truncated() -> None:
    error = api_error_from_response(make_text_response(500, "x" * 2000))

    assert error.detail == "x" * 500


def test_email_plain_text_error_body_becomes_the_message() -> None:
    error = email_error_from_response(make_text_response(415, "Expected request with `Content-Type: application/json`"))

    assert type(error) is APIStatusError
    assert str(error) == "415 Expected request with `Content-Type: application/json`"
    assert error.code is None


def test_email_structured_error() -> None:
    body = {"error": {"type": "bad_request", "message": "'to' must contain at least one recipient"}}

    error = email_error_from_response(make_response(400, body))

    assert isinstance(error, BadRequestError)
    assert error.code == "bad_request"
    assert str(error) == "400 'to' must contain at least one recipient"


def test_email_flat_auth_error() -> None:
    error = email_error_from_response(make_response(401, {"error": "Missing API key"}))

    assert isinstance(error, AuthenticationError)
    assert str(error) == "401 Missing API key"


def test_email_quota_error_exposes_quota() -> None:
    body = {"error": {"type": "email_quota_exceeded", "message": "Monthly quota used", "quota": {"limit": 3000}}}

    error = email_error_from_response(make_response(402, body))

    assert isinstance(error, EmailQuotaExceededError)
    assert error.quota == {"limit": 3000}


def test_email_billing_unavailable() -> None:
    body = {"error": {"type": "billing_unavailable", "message": "try later"}}

    error = email_error_from_response(make_response(503, body, headers={"Retry-After": "30"}))

    assert isinstance(error, ServiceUnavailableError)
    assert error.retry_after == 30.0


def test_email_upstream_error() -> None:
    error = email_error_from_response(make_response(502, {"error": {"type": "upstream_unavailable", "message": "x"}}))

    assert isinstance(error, UpstreamError)


def test_missing_secret_lists_keys() -> None:
    error = MissingSecretError(["A", "B"])

    assert error.keys == ["A", "B"]
    assert str(error) == "Missing environment variables: A, B"
