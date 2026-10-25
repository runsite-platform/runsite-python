"""Operations and the retry policy tested as plain generators: no network, no event loop, no respx.

`run_scripted` plays the driver's role with a finite list of scripted replies, so every loop here ends
when the script runs out.
"""

from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from runsite._dialects import api_error_from_response
from runsite._environment import Environment
from runsite._errors import ConflictError, NotFoundError
from runsite._http import Call, Operation, Request, RequestSettings, Wait, execute
from runsite._secrets.writer import SecretsOperations
from runsite._storage.control import create_key
from tests.constants import API_URL

SERVICE_ID = "svc-1"
SETTINGS = RequestSettings(
    base_url=API_URL, headers={}, timeout=5.0, max_retries=2, error_factory=api_error_from_response
)
Reply = httpx.Response | httpx.HTTPError


def respond(status_code: int, payload: Any = None) -> httpx.Response:
    request = httpx.Request("GET", API_URL)
    if payload is None:
        return httpx.Response(status_code, request=request)
    return httpx.Response(status_code, json=payload, request=request)


def run_scripted(operation: Operation[Any], replies: Sequence[Reply]) -> tuple[Any, list[Call | Wait]]:
    """Drive an operation with scripted replies; return its result and every step it asked for."""
    steps = execute(lambda: SETTINGS, operation)
    remaining = list(replies)
    seen: list[Call | Wait] = []
    reply: Any = None
    failure: Exception | None = None
    while True:
        try:
            step = steps.send(reply) if failure is None else steps.throw(failure)
        except StopIteration as stop:
            return stop.value, seen
        seen.append(step)
        reply, failure = None, None
        if isinstance(step, Wait):
            continue
        if not remaining:
            raise AssertionError(f"unexpected extra request: {step.method} {step.url}")
        scripted = remaining.pop(0)
        if isinstance(scripted, Exception):
            failure = scripted
        else:
            reply = scripted


def calls(steps: list[Call | Wait]) -> list[str]:
    return [f"{step.method} {step.url.removeprefix(API_URL)}" for step in steps if isinstance(step, Call)]


def listing(*variables: dict[str, Any]) -> dict[str, Any]:
    return {"environment_variables": list(variables)}


@pytest.fixture
def secrets() -> SecretsOperations:
    return SecretsOperations(Environment({}), service_id=SERVICE_ID)


def test_single_request_returns_parsed_body() -> None:
    def operation() -> Operation[Any]:
        return (yield Request("GET", "/thing"))

    result, steps = run_scripted(operation(), [respond(200, {"ok": True})])

    assert result == {"ok": True}
    assert calls(steps) == ["GET /thing"]


def test_get_is_retried_with_backoff_between_attempts() -> None:
    def operation() -> Operation[Any]:
        return (yield Request("GET", "/thing"))

    result, steps = run_scripted(operation(), [respond(503), respond(502), respond(200, {"ok": True})])

    assert result == {"ok": True}
    assert [type(step).__name__ for step in steps] == ["Call", "Wait", "Call", "Wait", "Call"]


def test_put_is_not_retried() -> None:
    def operation() -> Operation[Any]:
        return (yield Request("PUT", "/thing"))

    with pytest.raises(Exception, match="503"):
        run_scripted(operation(), [respond(503)])


def test_api_errors_are_thrown_into_the_operation() -> None:
    def operation() -> Operation[str]:
        try:
            yield Request("GET", "/missing")
        except NotFoundError:
            return "handled"
        return "not reached"

    result, _ = run_scripted(operation(), [respond(404, {"detail": "nope"})])

    assert result == "handled"


def test_min_timeout_raises_the_client_timeout() -> None:
    def operation() -> Operation[Any]:
        return (yield Request("DELETE", "/slow", min_timeout=120.0))

    _, steps = run_scripted(operation(), [respond(204)])

    assert isinstance(steps[0], Call)
    assert steps[0].timeout == 120.0


def test_set_many_repeats_read_and_write_once_after_a_conflict(secrets: SecretsOperations) -> None:
    replies = [
        respond(200, listing()),
        respond(409, {"detail": "exists"}),
        respond(200, listing({"id": "v1", "key": "TOKEN", "is_secret": True})),
        respond(200, {"updated": 1}),
    ]

    result, steps = run_scripted(secrets.set_many({"TOKEN": "value"}, None, None), replies)

    assert result.updated == 1
    assert calls(steps) == [
        f"GET /web-services/{SERVICE_ID}/env",
        f"PUT /web-services/{SERVICE_ID}/env/bulk",
        f"GET /web-services/{SERVICE_ID}/env",
        f"PUT /web-services/{SERVICE_ID}/env/bulk",
    ]


def test_set_many_gives_up_after_a_second_conflict(secrets: SecretsOperations) -> None:
    replies = [
        respond(200, listing()),
        respond(409, {"detail": "exists"}),
        respond(200, listing()),
        respond(409, {"detail": "exists"}),
    ]

    with pytest.raises(ConflictError):
        run_scripted(secrets.set_many({"TOKEN": "value"}, None, None), replies)


def test_set_many_validates_before_resolving_the_service() -> None:
    unconfigured = SecretsOperations(Environment({}), service_id=None)

    with pytest.raises(ValueError, match="not a valid key"):
        run_scripted(unconfigured.set_many({"bad key": "value"}, None, None), [])


def test_set_many_with_nothing_to_write_needs_no_configuration() -> None:
    unconfigured = SecretsOperations(Environment({}), service_id=None)

    result, steps = run_scripted(unconfigured.set_many({}, None, None), [])

    assert result.created == result.updated == result.deleted == 0
    assert steps == []


def test_delete_skips_the_write_when_no_key_exists(secrets: SecretsOperations) -> None:
    result, steps = run_scripted(secrets.delete(("MISSING",), None), [respond(200, listing())])

    assert result.deleted == 0
    assert calls(steps) == [f"GET /web-services/{SERVICE_ID}/env"]


def test_create_key_looks_up_the_bucket_id_by_name() -> None:
    bucket = {"id": "b1", "name": "avatars", "is_public": False, "created_at": "2026-10-22T10:00:00Z"}
    created = {
        "id": "k1",
        "access_key": "AKIA1",
        "secret_key": "secret",
        "permissions": {"read": True, "write": True, "delete": False},
        "bucket_id": "b1",
        "endpoint_url": "https://s3.runsite.test",
        "region": "auto",
    }

    result, steps = run_scripted(
        create_key("avatars", None, True, True, False), [respond(200, bucket), respond(201, created)]
    )

    assert result.bucket_id == "b1"
    assert calls(steps) == ["GET /storage/buckets/avatars", "POST /storage/keys"]
