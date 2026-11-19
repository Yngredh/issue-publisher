"""Eventos de log de "Contratos > Logs" (RNF14) com o PostgreSQL do testcontainers.

O GitHub é o adaptador httpx com respx (nenhuma chamada real), para conferir que o token não
aparece em log; o broker é trocado por uma exchange e uma mensagem em memória.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import aio_pika
import httpx
import pytest
import respx
import structlog

from issue_publisher.adapters.database import close_pool, create_pool, open_pool
from issue_publisher.adapters.github import HttpxGitHubIssues
from issue_publisher.consumer import Backoff, MessageHandler
from issue_publisher.domain.publishing import RetryPolicy
from tests.conftest import SeededProposal

API = "https://api.github.test"
TOKEN = "ghp_logs_test_token_never_logged"
BOT = "triage-bot"
POLICY = RetryPolicy(max_tries=3, delays_s=(10, 20), retry_after_max_s=60)
CASE_FIELDS = ("event_id", "repository", "issue_number", "revision")


class FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers: dict[str, Any] = {}
        self.content_type = "application/json"
        self.content_encoding = None
        self.message_id = None
        self.correlation_id = None
        self.calls: list[str] = []

    async def ack(self) -> None:
        self.calls.append("ack")

    async def nack(self, requeue: bool = True) -> None:
        self.calls.append(f"nack(requeue={requeue})")


class FakeExchange:
    def __init__(self) -> None:
        self.published: list[tuple[aio_pika.Message, str]] = []

    async def publish(self, message: aio_pika.Message, routing_key: str) -> None:
        self.published.append((message, routing_key))


@pytest.fixture
async def handler(database_url: str) -> AsyncIterator[MessageHandler]:
    pool = create_pool(database_url)
    await open_pool(pool)
    github = HttpxGitHubIssues(token=TOKEN, api_url=API, bot_login=BOT)

    async def wait(delay: float) -> None:
        await asyncio.sleep(0)

    try:
        yield MessageHandler(
            connections=pool,
            github=github,
            account=BOT,
            policy=POLICY,
            retry_exchange=FakeExchange(),  # type: ignore[arg-type]
            backoff=Backoff(1, 30),
            wait=wait,
        )
    finally:
        await github.aclose()
        await close_pool(pool)


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


def body_of(seeded: SeededProposal, revision: int | None = None) -> bytes:
    revision = seeded.revision if revision is None else revision
    return f'{{"proposalId": {seeded.proposal_id}, "revision": {revision}}}'.encode()


def comments_path(seeded: SeededProposal) -> str:
    return f"/repos/{seeded.repository}/issues/{seeded.issue_number}/comments"


def github_comment(seeded: SeededProposal, comment_id: int, login: str) -> dict[str, object]:
    return {
        "id": comment_id,
        "html_url": f"https://github.com/{seeded.repository}/issues/1#issuecomment-{comment_id}",
        "created_at": "2026-11-12T10:00:00Z",
        "user": {"login": login},
        "body": seeded.comment_body,
    }


async def handle(handler: MessageHandler, body: bytes) -> tuple[list[dict[str, Any]], FakeMessage]:
    message = FakeMessage(body)
    with structlog.testing.capture_logs() as logs:
        await handler(message)  # type: ignore[arg-type]
    return logs, message


def events(logs: list[dict[str, Any]]) -> list[str]:
    return [log["event"] for log in logs]


def only(logs: list[dict[str, Any]], event: str) -> dict[str, Any]:
    [log] = [log for log in logs if log["event"] == event]
    return log


def assert_case_fields(log: dict[str, Any], seeded: SeededProposal) -> None:
    assert log["event_id"] == seeded.origin_event_id
    assert log["repository"] == seeded.repository
    assert log["issue_number"] == seeded.issue_number
    assert log["revision"] == seeded.revision


def assert_no_secrets(logs: list[dict[str, Any]], seeded: SeededProposal) -> None:
    assert TOKEN not in repr(logs)
    assert str(seeded.comment_body) not in repr(logs)
    assert "Triagem automática" not in repr(logs)


async def test_published_flow_logs_received_try_and_published(
    handler: MessageHandler,
    api: respx.MockRouter,
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    seeded = make_committed_proposal()
    seen_context: list[dict[str, Any]] = []

    def create(request: httpx.Request) -> httpx.Response:
        seen_context.append(structlog.contextvars.get_contextvars())
        return httpx.Response(201, json=github_comment(seeded, 77, BOT))

    api.get(comments_path(seeded)).respond(200, json=[])
    api.post(comments_path(seeded)).mock(side_effect=create)

    logs, message = await handle(handler, body_of(seeded))

    assert message.calls == ["ack"]
    assert events(logs) == ["publish.received", "publish.try", "publish.published"]
    received = only(logs, "publish.received")
    assert received["proposal_id"] == seeded.proposal_id
    assert received["revision"] == seeded.revision
    for event in ("publish.try", "publish.published"):
        assert_case_fields(only(logs, event), seeded)
    assert only(logs, "publish.try")["try_number"] == 1
    assert only(logs, "publish.published")["try_number"] == 1
    assert only(logs, "publish.published")["github_comment_id"] == 77
    # `event_id` ligado ao contexto do structlog durante o processamento, e só durante ele.
    assert seen_context == [
        {
            "event_id": seeded.origin_event_id,
            "repository": seeded.repository,
            "issue_number": seeded.issue_number,
            "revision": seeded.revision,
        }
    ]
    assert "event_id" not in structlog.contextvars.get_contextvars()
    assert_no_secrets(logs, seeded)


async def test_marker_found_is_logged(
    handler: MessageHandler,
    api: respx.MockRouter,
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    seeded = make_committed_proposal()
    api.get(comments_path(seeded)).respond(200, json=[github_comment(seeded, 88, BOT)])
    post = api.post(comments_path(seeded))

    logs, _ = await handle(handler, body_of(seeded))

    assert events(logs) == ["publish.received", "publish.marker_found"]
    found = only(logs, "publish.marker_found")
    assert_case_fields(found, seeded)
    assert found["github_comment_id"] == 88
    assert not post.called
    assert_no_secrets(logs, seeded)


async def test_temporary_failure_logs_retry_scheduled_with_delay(
    handler: MessageHandler,
    api: respx.MockRouter,
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    seeded = make_committed_proposal()
    api.get(comments_path(seeded)).respond(200, json=[])
    api.post(comments_path(seeded)).respond(503, json={"message": f"down {TOKEN}"})

    logs, message = await handle(handler, body_of(seeded))

    assert message.calls == ["ack"]
    assert events(logs) == ["publish.received", "publish.try", "publish.retry_scheduled"]
    scheduled = only(logs, "publish.retry_scheduled")
    assert_case_fields(scheduled, seeded)
    assert scheduled["delay_s"] == 10
    assert scheduled["last_error"] == "http_503: down ***"
    assert_no_secrets(logs, seeded)


async def test_permanent_failure_logs_failed_with_kind_status_and_error(
    handler: MessageHandler,
    api: respx.MockRouter,
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    seeded = make_committed_proposal()
    api.get(comments_path(seeded)).respond(200, json=[])
    api.post(comments_path(seeded)).respond(
        403, json={"message": "Resource not accessible by integration"}
    )

    logs, _ = await handle(handler, body_of(seeded))

    assert events(logs) == ["publish.received", "publish.try", "publish.failed"]
    failed = only(logs, "publish.failed")
    assert_case_fields(failed, seeded)
    assert failed["failure_kind"] == "permanent"
    assert failed["http_status"] == 403
    assert failed["try_number"] == 1
    assert failed["last_error"] == "http_403: Resource not accessible by integration"
    assert failed["log_level"] == "warning"
    assert_no_secrets(logs, seeded)


async def test_precondition_failure_logs_failed(
    handler: MessageHandler,
    api: respx.MockRouter,
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    seeded = make_committed_proposal(in_catalog=False, repository="acme/not-in-catalog")

    logs, _ = await handle(handler, body_of(seeded))

    assert events(logs) == ["publish.received", "publish.failed"]
    failed = only(logs, "publish.failed")
    assert_case_fields(failed, seeded)
    assert failed["failure_kind"] == "permanent"
    assert failed["last_error"].startswith("repository_not_in_catalog: ")
    assert not api.calls
    assert_no_secrets(logs, seeded)


async def test_stale_revision_logs_skipped_stale(
    handler: MessageHandler,
    api: respx.MockRouter,
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    seeded = make_committed_proposal(revision=1, current_attempt=2)

    logs, message = await handle(handler, body_of(seeded))

    assert message.calls == ["ack"]
    assert events(logs) == ["publish.received", "publish.skipped_stale"]
    assert_case_fields(only(logs, "publish.skipped_stale"), seeded)
    assert not api.calls


async def test_invalid_messages_log_discarded_invalid(
    handler: MessageHandler,
    api: respx.MockRouter,
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    seeded = make_committed_proposal()

    invalid_logs, _ = await handle(handler, b"not json")
    mismatch_logs, _ = await handle(handler, body_of(seeded, revision=seeded.revision + 1))

    # D15: `publish.received` só sai depois de validar o schema.
    assert events(invalid_logs) == ["publish.discarded_invalid"]
    assert events(mismatch_logs) == ["publish.received", "publish.discarded_invalid"]
    discarded = only(mismatch_logs, "publish.discarded_invalid")
    assert discarded["event_id"] == seeded.origin_event_id
    assert discarded["repository"] == seeded.repository
    assert discarded["issue_number"] == seeded.issue_number
    assert discarded["revision"] == seeded.revision + 1
    assert not api.calls
    assert "event_id" not in structlog.contextvars.get_contextvars()
