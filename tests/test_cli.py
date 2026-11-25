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


def test_real_collection_is_blocked_until_the_real_judge_exists():
    with pytest.raises(SystemExit, match="etapa 4"):
        main(["collect"])


def test_live_test_limit_is_capped():
    with pytest.raises(SystemExit, match="--limit"):
        main(["collect", "--dry-run", "--live", "--limit", "50"])
