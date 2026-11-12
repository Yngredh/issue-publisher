"""Ack, nova tentativa e falhas de infraestrutura no `MessageHandler`, sem broker nem banco."""

from typing import Any

import aio_pika
import pytest
import structlog

from issue_publisher.consumer import (
    RETRY_QUEUE,
    Backoff,
    MessageHandler,
    ProcessResult,
)
from issue_publisher.domain.publish import PublishableProposal, ScreenOutcome
from issue_publisher.domain.publishing import PublishOutcome, PublishResult, RetryPolicy
from tests.fakes.github import FakeGitHubIssues

BODY = b'{"proposalId": 5, "revision": 1}'
PROPOSAL = PublishableProposal(
    proposal_id=5,
    repository="acme/payments-api",
    issue_number=7,
    origin_event_id="evt-1",
    revision=1,
    current_attempt=1,
    case_status="READY_TO_PUBLISH",
    kind="triage",
    validation_status="VALID",
    comment_body="body <!-- triage:evt-1:1 -->",
)


class FakeMessage:
    def __init__(self) -> None:
        self.body = BODY
        self.headers: dict[str, Any] = {"x-death": [{"count": 1}], "trace": "abc"}
        self.content_type = "application/json"
        self.content_encoding = None
        self.message_id = "m-1"
        self.correlation_id = None
        self.calls: list[str] = []

    async def ack(self) -> None:
        self.calls.append("ack")

    async def nack(self, requeue: bool = True) -> None:
        self.calls.append(f"nack(requeue={requeue})")


class FakeExchange:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[aio_pika.Message, str]] = []

    async def publish(self, message: aio_pika.Message, routing_key: str) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.published.append((message, routing_key))


class StubHandler(MessageHandler):
    """Troca os passos 1 a 9 (banco) por um resultado programado."""

    def __init__(self, result: ProcessResult | Exception, exchange: FakeExchange) -> None:
        self.waits: list[float] = []

        async def wait(delay: float) -> None:
            self.waits.append(delay)

        super().__init__(
            connections=None,  # type: ignore[arg-type]
            github=FakeGitHubIssues(),
            account="triage-bot",
            policy=RetryPolicy(max_tries=3, delays_s=(10, 20), retry_after_max_s=60),
            retry_exchange=exchange,  # type: ignore[arg-type]
            backoff=Backoff(1, 30),
            wait=wait,
        )
        self._result = result

    async def process(self, body: bytes) -> ProcessResult:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def retry_result(delay_s: float) -> ProcessResult:
    publish = PublishResult(PublishOutcome.RETRY, 1, delay_s=delay_s, last_error="http_503: x")
    return ProcessResult(ScreenOutcome.PROCEED, PROPOSAL, publish)


@pytest.mark.parametrize(
    "result",
    [
        ProcessResult(ScreenOutcome.DISCARDED_INVALID),
        ProcessResult(ScreenOutcome.SKIPPED_STALE, PROPOSAL),
        ProcessResult(ScreenOutcome.PROCEED, PROPOSAL, PublishResult(PublishOutcome.PUBLISHED, 1)),
        ProcessResult(ScreenOutcome.PROCEED, PROPOSAL, PublishResult(PublishOutcome.FAILED, 1)),
        ProcessResult(
            ScreenOutcome.PROCEED, PROPOSAL, PublishResult(PublishOutcome.ALREADY_FINAL, 1)
        ),
    ],
)
async def test_final_results_are_acked_without_retry_copy(result: ProcessResult) -> None:
    exchange = FakeExchange()
    message = FakeMessage()

    await StubHandler(result, exchange)(message)  # type: ignore[arg-type]

    assert message.calls == ["ack"]
    assert exchange.published == []


async def test_retry_publishes_copy_with_expiration_then_acks() -> None:
    exchange = FakeExchange()
    message = FakeMessage()

    with structlog.testing.capture_logs() as logs:
        await StubHandler(retry_result(20), exchange)(message)  # type: ignore[arg-type]

    assert message.calls == ["ack"]
    [(copy, routing_key)] = exchange.published
    assert routing_key == RETRY_QUEUE
    assert copy.body == BODY
    assert copy.expiration == 20
    assert copy.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert copy.headers == {"trace": "abc"}
    [scheduled] = [log for log in logs if log["event"] == "publish.retry_scheduled"]
    assert scheduled["delay_s"] == 20
    assert scheduled["event_id"] == "evt-1"
    assert scheduled["repository"] == "acme/payments-api"
    assert scheduled["issue_number"] == 7
    assert scheduled["revision"] == 1


async def test_retry_copy_not_confirmed_does_not_ack_the_original() -> None:
    message = FakeMessage()
    handler = StubHandler(retry_result(10), FakeExchange(fail=True))

    await handler(message)  # type: ignore[arg-type]

    assert message.calls == ["nack(requeue=True)"]
    assert handler.waits == [1]


async def test_infrastructure_failure_requeues_with_growing_backoff() -> None:
    message = FakeMessage()
    handler = StubHandler(OSError("database down"), FakeExchange())

    with structlog.testing.capture_logs() as logs:
        await handler(message)  # type: ignore[arg-type]
        await handler(message)  # type: ignore[arg-type]

    assert message.calls == ["nack(requeue=True)", "nack(requeue=True)"]
    assert handler.waits == [1, 2]
    assert [log["event"] for log in logs] == ["publish.processing_failed"] * 2


def test_backoff_doubles_up_to_the_maximum_and_resets() -> None:
    backoff = Backoff(1, 5)
    assert [backoff.next_delay() for _ in range(5)] == [1, 2, 4, 5, 5]
    backoff.reset()
    assert backoff.next_delay() == 1
