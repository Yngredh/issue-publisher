import asyncio
import os
import signal
import sys

import pytest
import structlog

from issue_publisher import main as main_module
from issue_publisher.config import Settings
from issue_publisher.domain.github import ErrorClass, GitHubError
from issue_publisher.worker import AccountIdentificationError, Worker, install_stop_handlers
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


UNREACHABLE_BROKER = "amqp://guest:guest@127.0.0.1:1/"


def http_error(status: int, error_class: ErrorClass, detail: str) -> GitHubError:
    return GitHubError(error_class, f"http_{status}", detail, http_status=status)


async def test_permanent_identify_failure_stops_without_connecting_to_broker(
    settings: Settings,
) -> None:
    """D15, item 3: 401 no `GET /user` encerra com erro, sem conectar ao broker."""
    github = FakeGitHubIssues()
    github.program_identify(http_error(401, ErrorClass.PERMANENT, "Bad credentials"))
    unreachable = settings.model_copy(update={"RABBITMQ_URL": UNREACHABLE_BROKER})
    worker = Worker(unreachable, github=github, connections=_NoDatabase(), backoff_initial_s=0.05)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(AccountIdentificationError):
            await asyncio.wait_for(worker.run(), timeout=5)

    events = [entry["event"] for entry in logs]
    assert events == ["worker.started", "worker.identify_failed", "worker.stopped"]
    failed = logs[1]
    assert failed["log_level"] == "error"
    assert failed["failure_kind"] == "permanent"
    assert failed["http_status"] == 401
    assert failed["last_error"] == "http_401: Bad credentials"
    assert github.identify_count == 1
    assert not worker.consuming.is_set()
    assert TOKEN not in repr(logs)


async def test_temporary_identify_failure_retries_until_stop_requested(
    settings: Settings,
) -> None:
    """D15, item 3: falha temporária tenta de novo com backoff e respeita o pedido de parada."""
    github = FakeGitHubIssues()
    github.program_identify(
        *[http_error(503, ErrorClass.TEMPORARY, "Service Unavailable") for _ in range(100)]
    )
    unreachable = settings.model_copy(update={"RABBITMQ_URL": UNREACHABLE_BROKER})
    worker = Worker(
        unreachable,
        github=github,
        connections=_NoDatabase(),
        backoff_initial_s=0.05,
        backoff_max_s=0.1,
    )

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(worker.run())
        for _ in range(100):
            if github.identify_count >= 3:
                break
            await asyncio.sleep(0.05)
        assert not task.done()
        worker.request_stop()
        await asyncio.wait_for(task, timeout=5)

    failures = [entry for entry in logs if entry["event"] == "worker.identify_failed"]
    assert len(failures) >= 2
    assert all(entry["log_level"] == "warning" for entry in failures)
    assert failures[0]["failure_kind"] == "temporary"
    assert failures[0]["http_status"] == 503
    assert [entry["backoff_s"] for entry in failures[:2]] == [0.05, 0.1]
    events = [entry["event"] for entry in logs]
    assert "worker.broker_unavailable" not in events
    assert events[-1] == "worker.stopped"
    assert TOKEN not in repr(logs)


def test_main_exits_non_zero_on_permanent_identify_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D15, item 3: falha permanente no `GET /user` encerra o processo com código diferente de 0."""
    monkeypatch.chdir("/")  # evita ler um .env local
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.setenv("RABBITMQ_URL", UNREACHABLE_BROKER)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)

    async def failing_run_worker(_settings: Settings) -> None:
        raise AccountIdentificationError(http_error(401, ErrorClass.PERMANENT, "Bad credentials"))

    monkeypatch.setattr(main_module, "run_worker", failing_run_worker)
    monkeypatch.setattr(main_module, "configure_logging", lambda _settings: None)

    with pytest.raises(SystemExit) as exit_info:
        main_module.main()

    assert exit_info.value.code not in (0, None)
