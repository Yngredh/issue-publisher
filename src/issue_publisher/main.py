import asyncio
import sys

from issue_publisher.config import Settings
from issue_publisher.logging import configure_logging
from issue_publisher.worker import AccountIdentificationError, run_worker


def main() -> None:
    """Inicia o worker; falha permanente ao identificar a conta encerra com código 1 (D15)."""
    settings = Settings()
    configure_logging(settings)
    try:
        if sys.platform == "win32":
            # O psycopg assíncrono não funciona com o ProactorEventLoop, padrão no Windows.
            asyncio.run(run_worker(settings), loop_factory=asyncio.SelectorEventLoop)
        else:
            asyncio.run(run_worker(settings))
    except AccountIdentificationError:
        sys.exit(1)  # o motivo já foi registrado no log `worker.identify_failed`


if __name__ == "__main__":
    main()
