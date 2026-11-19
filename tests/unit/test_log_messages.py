import json

import pytest
import structlog

from issue_publisher.config import Settings
from issue_publisher.log_messages import MAIN_FLOW_EVENTS, MESSAGES, add_message
from issue_publisher.logging import configure_logging


@pytest.mark.parametrize("event", MAIN_FLOW_EVENTS)
def test_main_flow_event_has_friendly_message(event: str) -> None:
    assert MESSAGES[event].strip()
    assert MESSAGES[event] != event


def test_add_message_known_unknown_and_explicit() -> None:
    known = add_message(None, "info", {"event": "publish.published"})
    assert known["message"] == MESSAGES["publish.published"]
    assert add_message(None, "info", {"event": "x.unknown"})["message"] == "x.unknown"
    explicit = add_message(None, "info", {"event": "publish.published", "message": "meu texto"})
    assert explicit["message"] == "meu texto"


def test_log_line_has_module_func_name_and_message(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir("/")  # evita ler um .env local
    settings = Settings(
        DATABASE_URL="postgresql://user:pass@localhost:5432/db",
        RABBITMQ_URL="amqp://guest:guest@localhost:5672/",
        GITHUB_TOKEN="token-de-teste",
    )
    try:
        configure_logging(settings)

        def emit_log() -> None:
            structlog.get_logger().info("publish.received", proposal_id=7, revision=2)

        emit_log()
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()

    assert line["event"] == "publish.received"
    assert line["message"] == MESSAGES["publish.received"]
    assert line["module"] == "test_log_messages"
    assert line["func_name"] == "emit_log"
    assert line["service"] == "issue-publisher"
    assert line["level"] == "info"
    assert line["proposal_id"] == 7
