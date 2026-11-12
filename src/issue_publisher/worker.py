import asyncio
import signal
import sys
from collections.abc import Callable

import aio_pika
import structlog
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractRobustConnection
from aio_pika.exceptions import CONNECTION_EXCEPTIONS, ChannelNotFoundEntity

from issue_publisher.adapters.database import close_pool, create_pool, open_pool
from issue_publisher.adapters.github import HttpxGitHubIssues
from issue_publisher.config import Settings
from issue_publisher.consumer import (
    PUBLISH_QUEUE,
    RETRY_QUEUE,
    RETRY_QUEUE_ARGUMENTS,
    Backoff,
    ConnectionSource,
    MessageHandler,
    sleep_unless,
)
from issue_publisher.domain.github import GitHubIssuesPort
from issue_publisher.domain.publishing import RetryPolicy

logger = structlog.get_logger()

DEFAULT_BACKOFF_INITIAL_S = 1.0
DEFAULT_BACKOFF_MAX_S = 30.0


def install_stop_handlers(loop: asyncio.AbstractEventLoop, stop: Callable[[], None]) -> None:
    """Liga SIGTERM e SIGINT à função `stop`.

    No Windows o loop não suporta `add_signal_handler`; o handler do módulo `signal`
    agenda `stop` no loop de forma segura entre threads.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        if sys.platform == "win32":
            signal.signal(sig, lambda _signum, _frame: loop.call_soon_threadsafe(stop))
        else:
            loop.add_signal_handler(sig, stop)


class Worker:
    """Worker sem HTTP do issue-publisher (D4), consumidor da fila `publish` (RF25, RNF03).

    Na subida identifica a conta do publisher, declara `publish.retry` e consome `publish` com
    declaração passiva, tentando de novo com backoff enquanto ela não existir (D6). Ao receber o
    pedido de parada, deixa de consumir, termina a mensagem em curso e fecha as conexões.

    `github` e `connections` permitem injetar a porta falsa e o banco nos testes; sem eles, o
    worker cria o cliente httpx e o pool do psycopg a partir do `Settings`.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        github: GitHubIssuesPort | None = None,
        connections: ConnectionSource | None = None,
        backoff_initial_s: float = DEFAULT_BACKOFF_INITIAL_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
    ) -> None:
        self._settings = settings
        self._github = github
        self._connections = connections
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._stop = asyncio.Event()
        self._wait = sleep_unless(self._stop)
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self.consuming = asyncio.Event()

    def request_stop(self) -> None:
        """Pede o encerramento limpo do worker."""
        if not self._stop.is_set():
            logger.info("worker.stop_requested")
            self._stop.set()

    async def run(self) -> None:
        """Executa até o pedido de parada."""
        logger.info("worker.started", log_level=self._settings.LOG_LEVEL)
        owned_github = None
        github = self._github
        if github is None:
            github = owned_github = HttpxGitHubIssues.from_settings(self._settings)
        pool = None
        connections = self._connections
        if connections is None:
            pool = create_pool(self._settings.DATABASE_URL)
            await open_pool(pool)
            connections = pool
        connection: AbstractRobustConnection | None = None
        try:
            account = await github.identify_account()
            connection = await self._connect()
            if connection is not None:
                await self._consume(connection, connections, github, account)
        finally:
            if connection is not None:
                await connection.close()
            if pool is not None:
                await close_pool(pool)
            if owned_github is not None:
                await owned_github.aclose()
            logger.info("worker.stopped")

    async def _connect(self) -> AbstractRobustConnection | None:
        """Conecta ao RabbitMQ, tentando de novo com backoff; `None` se a parada vier antes."""
        backoff = self._backoff()
        while not self._stop.is_set():
            try:
                return await aio_pika.connect_robust(self._settings.RABBITMQ_URL)
            except CONNECTION_EXCEPTIONS as error:
                delay = backoff.next_delay()
                logger.warning(
                    "worker.broker_unavailable", error=type(error).__name__, backoff_s=delay
                )
                await self._wait(delay)
        return None

    async def _wait_for_publish_queue(self, connection: AbstractRobustConnection) -> bool:
        """Declaração passiva de `publish` até ela existir (D6); `False` se a parada vier antes."""
        backoff = self._backoff()
        while not self._stop.is_set():
            probe = await connection.channel(publisher_confirms=False)
            try:
                await probe.declare_queue(PUBLISH_QUEUE, passive=True)
                return True
            except ChannelNotFoundEntity:
                delay = backoff.next_delay()
                logger.warning("worker.publish_queue_missing", backoff_s=delay)
            finally:
                if not probe.is_closed:
                    await probe.close()
            await self._wait(delay)
        return False

    async def _consume(
        self,
        connection: AbstractRobustConnection,
        connections: ConnectionSource,
        github: GitHubIssuesPort,
        account: str,
    ) -> None:
        channel: AbstractChannel = await connection.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=self._settings.PUBLISH_PREFETCH)
        await channel.declare_queue(RETRY_QUEUE, durable=True, arguments=RETRY_QUEUE_ARGUMENTS)
        if not await self._wait_for_publish_queue(connection):
            return
        queue = await channel.declare_queue(PUBLISH_QUEUE, passive=True)

        handler = MessageHandler(
            connections=connections,
            github=github,
            account=account,
            policy=RetryPolicy.from_settings(self._settings),
            retry_exchange=channel.default_exchange,
            backoff=self._backoff(),
            wait=self._wait,
        )

        async def on_message(message: AbstractIncomingMessage) -> None:
            self._in_flight += 1
            self._idle.clear()
            try:
                await handler(message)
            finally:
                self._in_flight -= 1
                if self._in_flight == 0:
                    self._idle.set()

        consumer_tag = await queue.consume(on_message, no_ack=False)
        logger.info(
            "worker.consuming", queue=PUBLISH_QUEUE, prefetch=self._settings.PUBLISH_PREFETCH
        )
        self.consuming.set()
        await self._stop.wait()
        try:
            await queue.cancel(consumer_tag)
        except CONNECTION_EXCEPTIONS as error:
            logger.warning("worker.cancel_failed", error=type(error).__name__)
        await self._idle.wait()

    def _backoff(self) -> Backoff:
        return Backoff(self._backoff_initial_s, self._backoff_max_s)


async def run_worker(settings: Settings) -> None:
    """Cria o worker, liga os sinais de parada e executa até o encerramento."""
    worker = Worker(settings)
    install_stop_handlers(asyncio.get_running_loop(), worker.request_stop)
    await worker.run()
