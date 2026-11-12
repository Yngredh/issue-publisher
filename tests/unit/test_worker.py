import asyncio
import os
import signal
import sys

import pytest
import structlog

from issue_publisher.config import Settings
from issue_publisher.worker import Worker, install_stop_handlers
from tests.fakes.github import FakeGitHubIssues

TOKEN = "ghp_test_token_never_logged"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.chdir("/")  # evita ler um .env local
    return Settings(
        DATABASE_URL="postgresql://user:pass@localhost:5432/db",
        RABBITMQ_URL="amqp://guest:guest@localhost:5672/",
        GITHUB_TOKEN=TOKEN,
    )


class _NoDatabase:
    """Banco que nunca é usado: sem broker, nenhuma mensagem chega."""

    def connection(self) -> object:
        raise AssertionError("database must not be used without messages")


async def test_worker_retries_broker_until_stop_requested(settings: Settings) -> None:
    unreachable = settings.model_copy(update={"RABBITMQ_URL": "amqp://guest:guest@127.0.0.1:1/"})
    worker = Worker(
        unreachable, github=FakeGitHubIssues(), connections=_NoDatabase(), backoff_initial_s=0.05
    )

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(worker.run())
        for _ in range(100):
            if sum(entry["event"] == "worker.broker_unavailable" for entry in logs) >= 2:
                break
            await asyncio.sleep(0.05)
        assert not task.done()
        worker.request_stop()
        await asyncio.wait_for(task, timeout=5)

    events = [entry["event"] for entry in logs]
    assert events[0] == "worker.started"
    assert events.count("worker.broker_unavailable") >= 2
    assert "worker.stop_requested" in events
    assert events[-1] == "worker.stopped"
    assert not worker.consuming.is_set()
    assert TOKEN not in repr(logs)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signal_requests_stop(sig: signal.Signals) -> None:
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

    try:
        install_stop_handlers(loop, stopped.set)
        if sys.platform == "win32":
            handler = signal.getsignal(sig)
            assert callable(handler)
            handler(sig, None)
        else:
            os.kill(os.getpid(), sig)
        await asyncio.wait_for(stopped.wait(), timeout=1)
    finally:
        for s, handler in previous.items():
            if sys.platform != "win32":
                loop.remove_signal_handler(s)
            signal.signal(s, handler)
