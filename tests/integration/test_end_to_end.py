"""Ponta a ponta do worker: PostgreSQL e RabbitMQ reais (testcontainers), GitHub pela porta falsa.

Cobre o cenário 6 (parte do publisher: mensagens acumuladas com o worker parado) e a queda do
broker durante o consumo. As mensagens são enviadas pelo `scripts/publish_message.py`.
Nenhum teste chama o GitHub de verdade.
"""

import asyncio
import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import structlog
from aio_pika.abc import AbstractRobustConnection
from testcontainers.community.rabbitmq import RabbitMqContainer

from issue_publisher.config import Settings
from issue_publisher.consumer import RETRY_QUEUE
from issue_publisher.domain.github import ErrorClass, GitHubError
from issue_publisher.worker import Worker
from tests.conftest import SeededProposal
from tests.fakes.github import FakeGitHubIssues
from tests.integration.test_consumer import (  # noqa: F401 (fixtures `broker` e `make_settings`)
    BOT,
    TOKEN,
    broker,
    make_settings,
    publication_row,
    ready_count,
    running_worker,
    state_is,
    wait_until,
)

SCRIPT = Path(__file__).parents[2] / "scripts" / "publish_message.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("publish_message_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish_script = load_script()


async def send_with_script(rabbitmq_url: str, seeded: SeededProposal) -> None:
    await publish_script.publish_message(rabbitmq_url, seeded.proposal_id, seeded.revision)


async def test_messages_accumulated_while_worker_is_stopped_are_published_when_it_returns(
    database_url: str,
    rabbitmq_url: str,
    broker: AbstractRobustConnection,  # noqa: F811
    make_settings: Callable[..., Settings],  # noqa: F811
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """Cenário 6 (parte do publisher): nada se perde com o worker parado."""
    github = FakeGitHubIssues(bot_login=BOT)
    first = make_committed_proposal()

    async with running_worker(Worker(make_settings(), github=github)):
        await send_with_script(rabbitmq_url, first)
        await wait_until(state_is(database_url, first.proposal_id, "PUBLISHED"))

    # Worker parado: as mensagens ficam acumuladas na fila durável `publish`.
    pending = [make_committed_proposal() for _ in range(3)]
    for seeded in pending:
        await send_with_script(rabbitmq_url, seeded)
    await asyncio.sleep(0.5)
    assert await ready_count(broker) == 3
    assert all(publication_row(database_url, s.proposal_id) is None for s in pending)
    assert github.post_count == 1

    with structlog.testing.capture_logs() as logs:
        async with running_worker(Worker(make_settings(), github=github)):
            for seeded in pending:
                await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))
            await wait_until(lambda: _is_zero(ready_count(broker)))

    assert github.post_count == 4
    for seeded in [first, *pending]:
        assert len(github.comments_on(seeded.repository, seeded.issue_number)) == 1
        row = publication_row(database_url, seeded.proposal_id)
        assert row is not None
        assert row["tries"] == 1
    assert TOKEN not in repr(logs)


async def test_broker_outage_during_consumption_does_not_lose_the_message(
    database_url: str,
    rabbitmq_url: str,
    rabbitmq_container: RabbitMqContainer,
    broker: AbstractRobustConnection,  # noqa: F811
    make_settings: Callable[..., Settings],  # noqa: F811
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """O broker cai com a mensagem em curso: ela não se perde nem gera um segundo comentário.

    O POST em curso termina com o broker fora e o commit no banco é feito (`PUBLISHED`,
    `tries = 1`); só o ack falha, porque o canal caiu. A mensagem não confirmada volta para a
    fila durável quando o broker retorna, e a reentrega encontra `PUBLISHED` no passo 5:
    confirma sem chamar o GitHub de novo (D15).
    """
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT, create_delay_s=3)
    worker = Worker(make_settings(), github=github, backoff_initial_s=0.1, backoff_max_s=0.5)

    with structlog.testing.capture_logs() as logs:
        async with running_worker(worker):
            await send_with_script(rabbitmq_url, seeded)
            await wait_until(lambda: github.post_count == 1)
            await _rabbitmqctl(rabbitmq_container, "stop_app")
            try:
                await asyncio.sleep(5)  # o POST em curso termina com o broker fora
            finally:
                await _rabbitmqctl(rabbitmq_container, "start_app")
            await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"), timeout=90)
            await wait_until(lambda: _ready_is_zero(broker), timeout=60)

    # A mensagem foi entregue de novo depois que o broker voltou e terminou confirmada.
    assert sum(log["event"] == "publish.received" for log in logs) == 2
    assert sum(log["event"] == "publish.published" for log in logs) == 1
    # O commit foi feito com o canal caído: só o ack falhou, sem `publish.processing_failed`.
    assert sum(log["event"] == "publish.ack_failed" for log in logs) == 1
    assert not any(log["event"] == "publish.processing_failed" for log in logs)
    # Um só POST e um só comentário: a reentrega encontra `PUBLISHED` no passo 5.
    assert github.post_count == 1
    assert len(github.comments_on(seeded.repository, seeded.issue_number)) == 1
    row = publication_row(database_url, seeded.proposal_id)
    assert row is not None
    assert row["tries"] == 1
    assert row["github_comment_id"] == github.comments_on(
        seeded.repository, seeded.issue_number
    )[0].id
    assert await ready_count(broker, RETRY_QUEUE) == 0
    assert TOKEN not in repr(logs)


async def test_temporary_identify_failure_retries_and_then_consumes(
    database_url: str,
    rabbitmq_url: str,
    broker: AbstractRobustConnection,  # noqa: F811
    make_settings: Callable[..., Settings],  # noqa: F811
    make_committed_proposal: Callable[..., SeededProposal],
) -> None:
    """D15, item 3: 503 no `GET /user` tenta de novo; com o 200, o worker segue e consome."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_identify(
        GitHubError(ErrorClass.TEMPORARY, "http_503", "Service Unavailable", http_status=503),
        None,
    )
    worker = Worker(make_settings(), github=github, backoff_initial_s=0.1, backoff_max_s=0.5)

    with structlog.testing.capture_logs() as logs:
        async with running_worker(worker):
            await send_with_script(rabbitmq_url, seeded)
            await wait_until(state_is(database_url, seeded.proposal_id, "PUBLISHED"))

    assert github.identify_count == 2
    failures = [log for log in logs if log["event"] == "worker.identify_failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "warning"
    assert failures[0]["http_status"] == 503
    assert github.post_count == 1
    assert TOKEN not in repr(logs)


async def _rabbitmqctl(container: RabbitMqContainer, command: str) -> None:
    result = await asyncio.to_thread(container.exec, ["rabbitmqctl", command])
    assert result.exit_code == 0, result.output


async def _is_zero(count: object) -> bool:
    return await count == 0  # type: ignore[misc]


async def _ready_is_zero(connection: AbstractRobustConnection) -> bool:
    try:
        return await ready_count(connection) == 0
    except Exception:  # conexão de teste ainda se restabelecendo depois da queda
        return False
