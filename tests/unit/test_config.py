import pytest
import structlog
from pydantic import ValidationError

from issue_publisher.config import Settings

TOKEN = "ghp_test_token_never_logged"

_ALL_VARS = (
    "LOG_LEVEL",
    "SERVICE_NAME",
    "DATABASE_URL",
    "RABBITMQ_URL",
    "GITHUB_TOKEN",
    "GITHUB_API_URL",
    "GITHUB_BOT_LOGIN",
    "GITHUB_CONNECT_TIMEOUT_S",
    "GITHUB_READ_TIMEOUT_S",
    "PUBLISH_MAX_TRIES",
    "PUBLISH_RETRY_DELAYS_S",
    "PUBLISH_RETRY_AFTER_MAX_S",
    "PUBLISH_PREFETCH",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir("/")  # evita ler um .env local
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)


def test_settings_load_with_defaults() -> None:
    settings = Settings()

    assert settings.LOG_LEVEL == "INFO"
    assert settings.SERVICE_NAME == "issue-publisher"
    assert settings.GITHUB_TOKEN.get_secret_value() == TOKEN
    assert settings.GITHUB_API_URL == "https://api.github.com"
    assert settings.GITHUB_BOT_LOGIN == ""
    assert settings.GITHUB_CONNECT_TIMEOUT_S == 10
    assert settings.GITHUB_READ_TIMEOUT_S == 15
    assert settings.PUBLISH_MAX_TRIES == 3
    assert settings.PUBLISH_RETRY_DELAYS_S == [10, 20]
    assert settings.PUBLISH_RETRY_AFTER_MAX_S == 60
    assert settings.PUBLISH_PREFETCH == 1


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "triage-bot")

    settings = Settings()

    assert settings.LOG_LEVEL == "DEBUG"
    assert settings.GITHUB_BOT_LOGIN == "triage-bot"


def test_retry_delays_parsed_from_comma_separated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLISH_RETRY_DELAYS_S", "1, 2")

    assert Settings().PUBLISH_RETRY_DELAYS_S == [1, 2]


def test_settings_fail_without_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")

    with pytest.raises(ValidationError, match="GITHUB_TOKEN"):
        Settings()


@pytest.mark.parametrize("value", ["0", "4"])
def test_settings_fail_with_max_tries_out_of_range(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PUBLISH_MAX_TRIES", value)

    with pytest.raises(ValidationError, match="PUBLISH_MAX_TRIES"):
        Settings()


def test_settings_fail_with_too_few_retry_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLISH_RETRY_DELAYS_S", "10")

    with pytest.raises(ValidationError, match="PUBLISH_RETRY_DELAYS_S"):
        Settings()


def test_fewer_tries_need_fewer_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLISH_MAX_TRIES", "2")
    monkeypatch.setenv("PUBLISH_RETRY_DELAYS_S", "10")

    assert Settings().PUBLISH_RETRY_DELAYS_S == [10]


def test_single_try_still_needs_one_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """D14 P-a: a falha temporária na busca do marcador (D12) sempre precisa de um atraso."""
    monkeypatch.setenv("PUBLISH_MAX_TRIES", "1")
    monkeypatch.setenv("PUBLISH_RETRY_DELAYS_S", "")

    with pytest.raises(ValidationError, match="PUBLISH_RETRY_DELAYS_S"):
        Settings()


def test_single_try_with_one_delay_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLISH_MAX_TRIES", "1")
    monkeypatch.setenv("PUBLISH_RETRY_DELAYS_S", "10")

    assert Settings().PUBLISH_RETRY_DELAYS_S == [10]


def test_token_not_in_repr() -> None:
    settings = Settings()

    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings)
    assert TOKEN not in repr(settings.model_dump())


def test_token_not_in_log() -> None:
    settings = Settings()

    with structlog.testing.capture_logs() as logs:
        structlog.get_logger().info("settings.loaded", settings=settings.model_dump())

    assert logs
    assert TOKEN not in repr(logs)
