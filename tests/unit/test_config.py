import pytest

from issue_publisher.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("SERVICE_NAME", raising=False)
    monkeypatch.chdir("/")  # avoid picking up a local .env


def test_settings_load_with_defaults() -> None:
    settings = Settings()

    assert settings.LOG_LEVEL == "INFO"
    assert settings.SERVICE_NAME == "issue-publisher"


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    assert Settings().LOG_LEVEL == "DEBUG"
