"""Classificação das respostas do GitHub e leitura do `Retry-After` (domínio, sem HTTP)."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from issue_publisher.domain.github import (
    ErrorClass,
    GitHubError,
    classify_http_error,
    connection_error,
    parse_retry_after,
    timeout_error,
)


def classify(status: int, headers: dict[str, str] | None = None) -> GitHubError:
    return classify_http_error(status, httpx.Headers(headers or {}), "detail")


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (500, {}, ErrorClass.TEMPORARY),
        (502, {}, ErrorClass.TEMPORARY),
        (503, {}, ErrorClass.TEMPORARY),
        (429, {}, ErrorClass.TEMPORARY),
        (429, {"Retry-After": "30"}, ErrorClass.TEMPORARY),
        (403, {"Retry-After": "30"}, ErrorClass.TEMPORARY),
        (403, {"X-RateLimit-Remaining": "0"}, ErrorClass.TEMPORARY),
        (403, {}, ErrorClass.PERMANENT),
        (403, {"X-RateLimit-Remaining": "4999"}, ErrorClass.PERMANENT),
        (401, {}, ErrorClass.PERMANENT),
        (404, {}, ErrorClass.PERMANENT),
        (410, {}, ErrorClass.PERMANENT),
        (422, {}, ErrorClass.PERMANENT),
        (400, {}, ErrorClass.PERMANENT),
    ],
)
def test_response_classes(status: int, headers: dict[str, str], expected: ErrorClass) -> None:
    error = classify(status, headers)

    assert error.error_class is expected
    assert error.temporary is (expected is ErrorClass.TEMPORARY)
    assert error.http_status == status
    assert error.code == f"http_{status}"


def test_last_error_format() -> None:
    error = classify_http_error(403, httpx.Headers(), "Resource not accessible")

    assert error.last_error == "http_403: Resource not accessible"
    assert str(error) == "http_403: Resource not accessible"


def test_retry_after_is_read() -> None:
    assert classify(429, {"Retry-After": "42"}).retry_after_s == 42
    assert classify(403, {"Retry-After": "7"}).retry_after_s == 7
    assert classify(503).retry_after_s is None


def test_retry_after_http_date() -> None:
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    value = format_datetime(now + timedelta(seconds=30), usegmt=True)

    assert parse_retry_after(value, now=now) == 30


@pytest.mark.parametrize("value", [None, "", "  ", "soon"])
def test_retry_after_missing_or_invalid(value: str | None) -> None:
    assert parse_retry_after(value) is None


def test_retry_after_in_the_past_is_zero() -> None:
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    value = format_datetime(now - timedelta(seconds=30), usegmt=True)

    assert parse_retry_after(value, now=now) == 0


def test_transport_errors_are_temporary() -> None:
    assert timeout_error("ReadTimeout").last_error == "timeout: ReadTimeout"
    assert timeout_error("ReadTimeout").temporary
    assert connection_error("ConnectError").last_error == "connection_error: ConnectError"
    assert connection_error("ConnectError").http_status is None
    assert connection_error("ConnectError").temporary
