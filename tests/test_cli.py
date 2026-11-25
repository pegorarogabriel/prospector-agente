"""A linha de comando, de ponta a ponta, sem banco."""

from prospector.cli import main


def test_import_legacy_dry_run_writes_nothing_and_reports(legacy_db, capsys):
    main(["import-legacy", str(legacy_db), "--dry-run"])
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "Posts lidos do banco antigo: 3" in out
    assert "legacy-agregado-truck" in out
    assert "legacy-agrega-toco" in out
