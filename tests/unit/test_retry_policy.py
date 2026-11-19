"""Atrasos da nova tentativa (D7 e D12)."""

import pytest

from issue_publisher.domain.publishing import RetryPolicy

POLICY = RetryPolicy(max_tries=3, delays_s=(10, 20), retry_after_max_s=60)


@pytest.mark.parametrize(("tries", "expected"), [(1, 10), (2, 20)])
def test_delay_after_try_uses_configured_delay(tries: int, expected: float) -> None:
    assert POLICY.delay_after_try(tries, None) == expected


@pytest.mark.parametrize(
    ("retry_after_s", "expected"),
    [(5, 10), (30, 30), (60, 60), (600, 60), (0, 10)],
)
def test_delay_after_try_with_retry_after(retry_after_s: float, expected: float) -> None:
    assert POLICY.delay_after_try(1, retry_after_s) == expected


@pytest.mark.parametrize(("tries", "expected"), [(0, 10), (1, 10), (2, 20)])
def test_delay_after_search_failure(tries: int, expected: float) -> None:
    assert POLICY.delay_after_search_failure(tries, None) == expected


def test_delay_after_search_failure_with_retry_after() -> None:
    assert POLICY.delay_after_search_failure(0, 45) == 45
