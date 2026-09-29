from __future__ import annotations

import os

from smart_money.bootstrap.paths import load_environment


def test_runtime_env_file_is_explicit_and_preserves_process_values(monkeypatch, tmp_path) -> None:
    source = tmp_path / ".env"
    source.write_text('SMART_MONEY_QUOTED="value # retained"\nSMART_MONEY_PRIORITY=from-file\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SMART_MONEY_ENV_FILE", raising=False)
    monkeypatch.delenv("SMART_MONEY_QUOTED", raising=False)
    monkeypatch.setenv("SMART_MONEY_PRIORITY", "from-process")

    load_environment()
    assert "SMART_MONEY_QUOTED" not in os.environ

    monkeypatch.setenv("SMART_MONEY_ENV_FILE", str(source))
    load_environment()
    assert os.environ["SMART_MONEY_QUOTED"] == "value # retained"
    assert os.environ["SMART_MONEY_PRIORITY"] == "from-process"
