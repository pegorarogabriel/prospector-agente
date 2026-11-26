"""A linha de comando, de ponta a ponta, sem banco."""

import pytest

from prospector.cli import main


def test_import_legacy_dry_run_writes_nothing_and_reports(legacy_db, capsys):
    main(["import-legacy", str(legacy_db), "--dry-run"])
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "Posts lidos do banco antigo: 3" in out
    assert "legacy-agregado-truck" in out
    assert "legacy-agrega-toco" in out


def test_live_flag_needs_dry_run():
    with pytest.raises(SystemExit, match="--live"):
        main(["collect", "--live"])


def test_live_test_limit_is_capped():
    with pytest.raises(SystemExit, match="--limit"):
        main(["collect", "--dry-run", "--live", "--limit", "50"])


@pytest.mark.parametrize("command", [["ingest-replies"], ["telegram", "ping"]])
def test_telegram_commands_need_the_token(command, monkeypatch):
    # load_dotenv não sobrescreve o que já está no ambiente: o .env local não vaza aqui.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        main(command)


def test_telegram_commands_need_the_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    with pytest.raises(SystemExit, match="telegram chats"):
        main(["ingest-replies"])


def test_real_collect_checks_telegram_before_spending(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        main(["collect"])
