"""Consumidor da fila `publish` com RabbitMQ e PostgreSQL do testcontainers e a porta falsa.

Atrasos curtos nos testes (`PUBLISH_RETRY_DELAYS_S=1,2`). Nenhum teste chama o GitHub de verdade.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import aio_pika
import psycopg
import pytest
import structlog
from aio_pika.abc import AbstractChannel, AbstractRobustConnection
from psycopg import AsyncConnection

from issue_publisher.adapters.database import close_pool, create_pool, open_pool
from issue_publisher.config import Settings
from issue_publisher.consumer import (
    PUBLISH_QUEUE,
    RETRY_QUEUE,
    RETRY_QUEUE_ARGUMENTS,
    ConnectionSource,
)
from issue_publisher.domain.github import ErrorClass, GitHubError
from issue_publisher.worker import Worker
from tests.conftest import SeededProposal
from tests.fakes.github import FakeGitHubIssues

BOT = "triage-bot"
TOKEN = "ghp_consumer_test_token_never_logged"


def http_error(status: int, message: str, retry_after_s: float | None = None) -> GitHubError:
    error_class = ErrorClass.TEMPORARY if status >= 500 or status == 429 else ErrorClass.PERMANENT
    return GitHubError(
        error_class, f"http_{status}", message, http_status=status, retry_after_s=retry_after_s
    )


def message_body(seeded: SeededProposal) -> bytes:
    return json.dumps({"proposalId": seeded.proposal_id, "revision": seeded.revision}).encode()


@pytest.fixture
def make_settings(
    database_url: str, rabbitmq_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Settings]:
    monkeypatch.chdir(tmp_path)  # evita ler um .env local

    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "DATABASE_URL": database_url,
            "RABBITMQ_URL": rabbitmq_url,
            "GITHUB_TOKEN": TOKEN,
            "GITHUB_BOT_LOGIN": BOT,
            "PUBLISH_RETRY_DELAYS_S": "1,2",
        }
        values.update(overrides)
        return Settings(**values)  # type: ignore[arg-type]

    return factory


@pytest.fixture
async def broker(rabbitmq_url: str) -> AsyncIterator[AbstractRobustConnection]:
    """Conexão de teste; recria `publish` como o issue-triage fará e apaga as filas ao fim."""
    connection = await aio_pika.connect_robust(rabbitmq_url)
    channel = await connection.channel()
    await channel.queue_delete(PUBLISH_QUEUE)
    await channel.queue_delete(RETRY_QUEUE)
    await channel.declare_queue(PUBLISH_QUEUE, durable=True)
    yield connection
    channel = await connection.channel()
    await channel.queue_delete(PUBLISH_QUEUE)
    await channel.queue_delete(RETRY_QUEUE)
    await connection.close()


async def send(connection: AbstractRobustConnection, body: bytes) -> None:
    channel = await connection.channel()
    await channel.default_exchange.publish(
        aio_pika.Message(body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
        routing_key=PUBLISH_QUEUE,
    )
    await channel.close()


async def ready_count(connection: AbstractRobustConnection, queue: str = PUBLISH_QUEUE) -> int:
    channel = await connection.channel()
    declared = await channel.declare_queue(queue, passive=True)
    await channel.close()
    count = declared.declaration_result.message_count
    assert count is not None
    return count


async def wait_until(predicate: Callable[[], Awaitable[bool] | bool], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if isinstance(value, Awaitable):
            value = await value
        if value:
            return
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.1)


@asynccontextmanager
async def running_worker(worker: Worker, *, wait_consuming: bool = True) -> AsyncIterator[Worker]:
    task = asyncio.create_task(worker.run())
    try:
        if wait_consuming:
            await asyncio.wait_for(worker.consuming.wait(), timeout=30)
        yield worker
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, timeout=30)


def publication_row(database_url: str, proposal_id: int) -> dict[str, object] | None:
    keys = "id state tries failure_kind last_error github_comment_id".split()
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(keys)} FROM publisher.publications WHERE proposal_id = %s",
            (proposal_id,),
        ).fetchall()
    assert len(rows) <= 1
    return dict(zip(keys, rows[0], strict=True)) if rows else None


def tries_rows(database_url: str, publication_id: object) -> list[tuple[object, ...]]:
    with psycopg.connect(database_url) as conn:
        return conn.execute(
            "SELECT try_number, http_status, error FROM publisher.publication_tries "
            "WHERE publication_id = %s ORDER BY try_number",
            (publication_id,),
        ).fetchall()


def state_is(database_url: str, proposal_id: int, state: str) -> Callable[[], bool]:
    def check() -> bool:
        row = publication_row(database_url, proposal_id)
        return row is not None and row["state"] == state

    return check


def retry_delays(logs: list[dict[str, object]]) -> list[object]:
    return [log["delay_s"] for log in logs if log["event"] == "publish.retry_scheduled"]


async def test_same_message_delivered_twice_posts_once(
    database_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """Cenário 2."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    await send(broker, message_body(seeded))
    await send(broker, message_body(seeded))

    async with running_worker(Worker(make_settings(), github=github)):
        await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))
        await wait_until(lambda: _is_zero(ready_count(broker)))
    assert github.post_count == 1
    assert len(github.comments_on(seeded.repository, seeded.issue_number)) == 1
    assert await ready_count(broker) == 0  # as duas entregas foram confirmadas


async def test_two_consumers_with_the_same_message_post_once(
    database_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """Cenário 4: o `FOR UPDATE` da reivindicação segura o segundo consumidor."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT, create_delay_s=0.5)

    async with (
        running_worker(Worker(make_settings(), github=github)),
        running_worker(Worker(make_settings(), github=github)),
    ):
        await send(broker, message_body(seeded))
        await send(broker, message_body(seeded))
        await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))
        await wait_until(lambda: _is_zero(ready_count(broker)))
    assert github.post_count == 1
    assert len(github.comments_on(seeded.repository, seeded.issue_number)) == 1
    assert await ready_count(broker) == 0


async def _is_zero(count: Awaitable[int]) -> bool:
    return await count == 0


async def test_two_503_then_201_publishes_on_third_try(
    database_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """Cenário 7: 503, 503 e 201 terminam em `PUBLISHED` com `tries = 3`."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_create(http_error(503, "first"), http_error(503, "second"))

    with structlog.testing.capture_logs() as logs:
        async with running_worker(Worker(make_settings(), github=github)):
            await send(broker, message_body(seeded))
            await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))

    row = publication_row(database_url, seeded.proposal_id)
    assert row is not None
    assert row["tries"] == 3
    assert tries_rows(database_url, row["id"]) == [(1, 503, None), (2, 503, None), (3, 201, None)]
    assert retry_delays(logs) == [1.0, 2.0]
    first_gap = github.post_times[1] - github.post_times[0]
    second_gap = github.post_times[2] - github.post_times[1]
    assert first_gap >= 0.9
    assert second_gap >= 1.9
    assert github.post_count == 3
    assert await ready_count(broker) == 0
    assert await ready_count(broker, RETRY_QUEUE) == 0
    assert TOKEN not in repr(logs)


async def test_three_5xx_exhaust_the_tries(
    database_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """Cenário 8: 5xx três vezes termina em `FAILED (retries_exhausted)`."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_create(http_error(503, "a"), http_error(502, "b"), http_error(500, "c"))

    with structlog.testing.capture_logs() as logs:
        async with running_worker(Worker(make_settings(), github=github)):
            await send(broker, message_body(seeded))
            await wait_until(state_is(database_url, seeded.proposal_id, "FAILED"))

    row = publication_row(database_url, seeded.proposal_id)
    assert row is not None
    assert row["failure_kind"] == "retries_exhausted"
    assert row["tries"] == 3
    assert row["last_error"] == "http_500: c"
    assert github.post_count == 3
    assert retry_delays(logs) == [1.0, 2.0]
    assert await ready_count(broker) == 0
    assert await ready_count(broker, RETRY_QUEUE) == 0


async def test_429_with_retry_after_waits_the_requested_time(
    database_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """Cenário 10: o atraso é o maior entre o configurado (1 s) e o `Retry-After` (3 s)."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_create(http_error(429, "Too Many Requests", retry_after_s=3))

    with structlog.testing.capture_logs() as logs:
        async with running_worker(Worker(make_settings(), github=github)):
            await send(broker, message_body(seeded))
            await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))

    assert retry_delays(logs) == [3.0]
    assert github.post_times[1] - github.post_times[0] >= 2.9
    row = publication_row(database_url, seeded.proposal_id)
    assert row is not None
    assert tries_rows(database_url, row["id"]) == [(1, 429, None), (2, 201, None)]


async def test_retry_copy_goes_to_publish_retry_with_expiration(
    database_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """A cópia vai para `publish.retry`, persistente e com a expiração da D7."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_create(http_error(503, "outage"))
    channel: AbstractChannel = await broker.channel()

    async with running_worker(Worker(make_settings(PUBLISH_RETRY_DELAYS_S="3,3"), github=github)):
        # Topologia da D6: redeclarar com os mesmos argumentos só funciona se forem iguais.
        await channel.declare_queue(RETRY_QUEUE, durable=True, arguments=RETRY_QUEUE_ARGUMENTS)
        retry_queue = await channel.declare_queue(RETRY_QUEUE, passive=True)
        await send(broker, message_body(seeded))

        copy = None
        deadline = time.monotonic() + 10
        while copy is None and time.monotonic() < deadline:
            copy = await retry_queue.get(no_ack=False, fail=False)
            if copy is None:
                await asyncio.sleep(0.05)
        assert copy is not None
        assert copy.body == message_body(seeded)
        assert copy.expiration == 3.0
        assert copy.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        await copy.nack(requeue=True)

        await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))
    row = publication_row(database_url, seeded.proposal_id)
    assert row is not None
    assert row["tries"] == 2
    assert github.post_count == 2


async def test_search_503_then_post_201_publishes_with_one_try(
    database_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """D12: a falha na busca não consome tentativa; o atraso é `PUBLISH_RETRY_DELAYS_S[0]`."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_find(http_error(503, "search down"))

    with structlog.testing.capture_logs() as logs:
        async with running_worker(Worker(make_settings(), github=github)):
            await send(broker, message_body(seeded))
            await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))

    row = publication_row(database_url, seeded.proposal_id)
    assert row is not None
    assert row["tries"] == 1
    assert tries_rows(database_url, row["id"]) == [(1, 201, None)]
    assert retry_delays(logs) == [1.0]
    assert github.find_count == 2
    assert github.post_count == 1


async def test_stale_revision_is_acked_without_effect(
    database_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """Cenário 13."""
    seeded = make_committed_proposal(revision=1, current_attempt=2)
    github = FakeGitHubIssues(bot_login=BOT)

    with structlog.testing.capture_logs() as logs:
        async with running_worker(Worker(make_settings(), github=github)):
            await send(broker, message_body(seeded))
            await wait_until(lambda: any(log["event"] == "publish.skipped_stale" for log in logs))

    assert publication_row(database_url, seeded.proposal_id) is None
    assert github.find_count == 0
    assert github.post_count == 0
    assert await ready_count(broker) == 0


async def test_invalid_schema_and_unknown_proposal_are_discarded(
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
) -> None:
    """Cenário 16."""
    github = FakeGitHubIssues(bot_login=BOT)
    bodies = [
        b"not json",
        b'{"proposalId": 1, "revision": 1, "extra": true}',
        b'{"proposalId": 999999999, "revision": 1}',
    ]

    with structlog.testing.capture_logs() as logs:
        async with running_worker(Worker(make_settings(), github=github)):
            for body in bodies:
                await send(broker, body)
            await wait_until(
                lambda: sum(log["event"] == "publish.discarded_invalid" for log in logs) == 3
            )

    assert github.find_count == 0
    assert github.post_count == 0
    assert await ready_count(broker) == 0
    assert await ready_count(broker, RETRY_QUEUE) == 0


class FlakyConnections:
    """Pool que falha como banco fora do ar nas primeiras `failures` chamadas."""

    def __init__(self, inner: ConnectionSource, failures: int) -> None:
        self._inner = inner
        self.failures_left = failures

    def connection(self) -> AbstractAsyncContextManager[AsyncConnection]:
        if self.failures_left > 0:
            self.failures_left -= 1
            raise psycopg.OperationalError("connection refused (simulated database outage)")
        return self._inner.connection()


async def test_database_down_does_not_ack_and_message_is_processed_when_it_returns(
    database_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """Banco parado: a mensagem não é confirmada, volta para a fila e não gasta tentativa."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    pool = create_pool(database_url)
    await open_pool(pool)
    connections = FlakyConnections(pool, failures=2)
    worker = Worker(make_settings(), github=github, connections=connections, backoff_initial_s=0.1)

    try:
        with structlog.testing.capture_logs() as logs:
            async with running_worker(worker):
                await send(broker, message_body(seeded))
                await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))
    finally:
        await close_pool(pool)

    failures = [log for log in logs if log["event"] == "publish.processing_failed"]
    assert [log["error"] for log in failures] == ["OperationalError", "OperationalError"]
    assert [log["backoff_s"] for log in failures] == [0.1, 0.2]
    row = publication_row(database_url, seeded.proposal_id)
    assert row is not None
    assert row["tries"] == 1
    assert github.post_count == 1
    assert await ready_count(broker) == 0


async def test_waits_for_publish_queue_declared_by_triage(
    database_url: str,
    rabbitmq_url: str,
    broker: AbstractRobustConnection,
    make_settings: Callable[..., Settings],
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """D6: sem a fila `publish`, o worker tenta a declaração passiva de novo com backoff."""
    channel = await broker.channel()
    await channel.queue_delete(PUBLISH_QUEUE)
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    worker = Worker(make_settings(), github=github, backoff_initial_s=0.1, backoff_max_s=0.5)

    with structlog.testing.capture_logs() as logs:
        async with running_worker(worker, wait_consuming=False):
            await wait_until(
                lambda: any(log["event"] == "worker.publish_queue_missing" for log in logs)
            )
            assert not worker.consuming.is_set()
            channel = await broker.channel()
            await channel.declare_queue(PUBLISH_QUEUE, durable=True)
            await asyncio.wait_for(worker.consuming.wait(), timeout=30)
            await send(broker, message_body(seeded))
            await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))

    assert github.post_count == 1
