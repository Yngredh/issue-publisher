import asyncio
import signal
import sys
from collections.abc import Callable

import structlog

from issue_publisher.config import Settings

logger = structlog.get_logger()


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
    """Worker sem HTTP do issue-publisher (D4).

    Fica ativo até `request_stop`. O consumo da fila `publish` entra na Etapa 7: ao
    receber o pedido de parada, deixa de consumir, termina a mensagem em curso e fecha
    as conexões.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Pede o encerramento limpo do worker."""
        if not self._stop.is_set():
            logger.info("worker.stop_requested")
            self._stop.set()

    async def run(self) -> None:
        """Executa até o pedido de parada."""
        logger.info("worker.started", log_level=self._settings.LOG_LEVEL)
        await self._stop.wait()
        logger.info("worker.stopped")


async def run_worker(settings: Settings) -> None:
    """Cria o worker, liga os sinais de parada e executa até o encerramento."""
    worker = Worker(settings)
    install_stop_handlers(asyncio.get_running_loop(), worker.request_stop)
    await worker.run()
