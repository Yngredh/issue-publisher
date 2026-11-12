import asyncio
import sys

from issue_publisher.config import Settings
from issue_publisher.logging import configure_logging
from issue_publisher.worker import run_worker


def main() -> None:
    settings = Settings()
    configure_logging(settings)
    if sys.platform == "win32":
        # O psycopg assíncrono não funciona com o ProactorEventLoop, padrão no Windows.
        asyncio.run(run_worker(settings), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(run_worker(settings))


if __name__ == "__main__":
    main()
