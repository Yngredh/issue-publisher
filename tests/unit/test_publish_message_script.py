"""Ferramenta `scripts/publish_message.py` sem broker."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "publish_message.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("publish_message_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_body_follows_the_publish_contract() -> None:
    script = load_script()
    assert json.loads(script.build_body(5, 1)) == {"proposalId": 5, "revision": 1}


def test_without_rabbitmq_url_exits_with_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # sem .env
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    script = load_script()
    assert script.main(["5", "1"]) == 2
    assert "RABBITMQ_URL" in capsys.readouterr().err


def test_rejects_non_integer_arguments() -> None:
    script = load_script()
    with pytest.raises(SystemExit):
        script.main(["abc", "1"])
