"""Tratamento de uma mensagem da fila `publish`: passos 1 a 10 (RF25, RNF03).

Abre a transação que cobre a tentativa inteira, roda os passos 1 a 3 (`screen_message`) e 4 a 9
(`publish_proposal`), faz o commit e só depois confirma a mensagem. Na nova tentativa, publica a
cópia em `publish.retry` com a expiração da D7 (ou da D12) e confirmação do broker, e só então
confirma a original. Banco ou broker indisponível: a mensagem não é confirmada e volta para a
fila, sem gastar tentativa (a transação é desfeita). Se o canal cair com a mensagem em curso, o
tratamento é protegido do cancelamento: o `POST` e o commit terminam e só o ack falha (D15).
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

import aio_pika
import structlog
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage
from psycopg import AsyncConnection

from issue_publisher.adapters.proposals import PostgresProposalReader
from issue_publisher.adapters.publications import PostgresPublicationStore
from issue_publisher.domain.github import GitHubIssuesPort
from issue_publisher.domain.publish import PublishableProposal, ScreenOutcome, screen_message
from issue_publisher.domain.publishing import (
    PublishOutcome,
    PublishResult,
    RetryPolicy,
    publish_proposal,
)

logger = structlog.get_logger()

PUBLISH_QUEUE = "publish"
RETRY_QUEUE = "publish.retry"
RETRY_QUEUE_ARGUMENTS = {
    "x-dead-letter-exchange": "",
    "x-dead-letter-routing-key": PUBLISH_QUEUE,
}


class ConnectionSource(Protocol):
    """Origem das conexões do banco (o `AsyncConnectionPool` do psycopg)."""

    def connection(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


@dataclass(frozen=True)
class ProcessResult:
    """Resultado dos passos 1 a 9, já com a transação confirmada."""

    screen: ScreenOutcome
    proposal: PublishableProposal | None = None
    publish: PublishResult | None = None

    @property
    def retry_delay_s(self) -> float | None:
        """Atraso da cópia em `publish.retry`, ou `None` quando não há nova tentativa."""
        if self.publish is not None and self.publish.outcome is PublishOutcome.RETRY:
            return self.publish.delay_s
        return None


class Backoff:
    """Espera crescente entre falhas seguidas de banco ou broker (dobra até o máximo)."""

    def __init__(self, initial_s: float, max_s: float) -> None:
        self._initial_s = initial_s
        self._max_s = max_s
        self._failures = 0

    def next_delay(self) -> float:
        delay = min(self._initial_s * (2**self._failures), self._max_s)
        self._failures += 1
        return delay

    def reset(self) -> None:
        self._failures = 0


class MessageHandler:
    """Liga os passos 1 a 10 a cada mensagem recebida da fila `publish`."""

    def __init__(
        self,
        *,
        connections: ConnectionSource,
        github: GitHubIssuesPort,
        account: str,
        policy: RetryPolicy,
        retry_exchange: AbstractExchange,
        backoff: Backoff,
        wait: Callable[[float], Awaitable[None]],
    ) -> None:
        self._connections = connections
        self._github = github
        self._account = account
        self._policy = policy
        self._retry_exchange = retry_exchange
        self._backoff = backoff
        self._wait = wait

    async def process(self, body: bytes) -> ProcessResult:
        """Passos 1 a 9 numa transação; o commit acontece na saída do bloco (passo 10).

        O `event_id` do caso fica ligado ao contexto do structlog só durante a mensagem.
        """
        with _restored_log_context():
            return await self._process(body)

    async def _process(self, body: bytes) -> ProcessResult:
        async with self._connections.connection() as conn:
            async with conn.transaction():
                reader = PostgresProposalReader(conn)
                screen = await screen_message(body, reader)
                if screen.outcome is not ScreenOutcome.PROCEED or screen.proposal is None:
                    return ProcessResult(screen.outcome, screen.proposal)
                result = await publish_proposal(
                    screen.proposal,
                    PostgresPublicationStore(conn),
                    reader,
                    self._github,
                    account=self._account,
                    policy=self._policy,
                )
                return ProcessResult(screen.outcome, screen.proposal, result)

    async def __call__(self, message: AbstractIncomingMessage) -> None:
        """Trata a mensagem protegida do cancelamento do aio-pika (D4, D15).

        Quando o canal cai, o aio-pika cancela o callback em curso. O tratamento roda numa tarefa
        protegida por `asyncio.shield`: o `POST` e o commit terminam e só o ack falha. O
        cancelamento é repassado depois que o tratamento acaba, e a reentrega encontra o
        resultado gravado (`PUBLISHED` no passo 5).
        """
        handling = asyncio.ensure_future(self._handle(message))
        cancelled = False
        while True:
            try:
                await asyncio.shield(handling)
                break
            except asyncio.CancelledError:
                if handling.done():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        """Processa, faz o commit e só então confirma; falha de infraestrutura não confirma.

        Ack que falha depois do commit gera `publish.ack_failed`, sem backoff: o resultado já
        está gravado e a mensagem volta sozinha para a fila quando o canal cai.
        """
        try:
            result = await self.process(message.body)
            delay_s = result.retry_delay_s
            if delay_s is not None:
                await self._schedule_retry(message, result, delay_s)
        except Exception as error:  # qualquer falha: não confirma a mensagem
            delay = self._backoff.next_delay()
            logger.warning("publish.processing_failed", error=type(error).__name__, backoff_s=delay)
            await self._wait(delay)
            await _requeue(message)
            return
        self._backoff.reset()
        try:
            await message.ack()
        except Exception as error:  # canal fechado: o broker devolve a mensagem sozinho
            logger.warning("publish.ack_failed", error=type(error).__name__)

    async def _schedule_retry(
        self, message: AbstractIncomingMessage, result: ProcessResult, delay_s: float
    ) -> None:
        """Publica a cópia em `publish.retry` com expiração e espera a confirmação do broker."""
        headers = {k: v for k, v in (message.headers or {}).items() if not k.startswith("x-")}
        copy = aio_pika.Message(
            body=message.body,
            headers=headers,
            content_type=message.content_type,
            content_encoding=message.content_encoding,
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            expiration=delay_s,
        )
        await self._retry_exchange.publish(copy, routing_key=RETRY_QUEUE)
        proposal = result.proposal
        publication = result.publish
        logger.info(
            "publish.retry_scheduled",
            event_id=proposal.origin_event_id if proposal else None,
            repository=proposal.repository if proposal else None,
            issue_number=proposal.issue_number if proposal else None,
            revision=proposal.revision if proposal else None,
            delay_s=delay_s,
            last_error=publication.last_error if publication else None,
        )


@contextmanager
def _restored_log_context() -> Iterator[None]:
    """Restaura o contexto do structlog ao fim da mensagem (o `event_id` não vaza)."""
    saved = structlog.contextvars.get_contextvars()
    try:
        yield
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**saved)


async def _requeue(message: AbstractIncomingMessage) -> None:
    """Devolve a mensagem para a fila; com o broker fora, ela volta quando o canal cai."""
    try:
        await message.nack(requeue=True)
    except Exception as error:  # canal fechado: o broker devolve sozinho
        logger.warning("publish.requeue_failed", error=type(error).__name__)


def sleep_unless(stop: asyncio.Event) -> Callable[[float], Awaitable[None]]:
    """Espera `delay` segundos ou até o pedido de parada, o que vier primeiro."""

    async def wait(delay: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    return wait
