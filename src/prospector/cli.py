"""Linha de comando: `uv run prospector ...`.

Este é o ponto de montagem (composition root). É o único módulo que conhece ao
mesmo tempo os flows e as integrações: ele escolhe a implementação concreta de
cada porta e a entrega ao caso de uso. Os flows nunca sabem se estão falando
com o Postgres ou com a memória.
"""

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path

from dotenv import load_dotenv

from prospector.flows.import_legacy import LEGACY_PRIOR_WEIGHT, ImportReport, import_legacy
from prospector.flows.ports import Repository


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url or "user:senha@host" in url:
        sys.exit("DATABASE_URL não configurada. Copie .env.example para .env e preencha.")
    return url


def _print_report(report: ImportReport, repository: Repository) -> None:
    cutoff = report.cutoff.strftime("%d/%m/%Y %H:%M UTC") if report.cutoff else "sem corte"
    print(f"Posts lidos do banco antigo: {report.posts_read}")
    print(f"  corte (última resposta):   {cutoff}")
    print(f"  depois do corte (fora):    {report.posts_after_cutoff}")
    print(f"  sem texto nem imagem:      {report.posts_without_content}")
    print(f"Entraram agora: {report.recipes_added} receitas, {report.posts_added} posts, ", end="")
    print(f"{report.triage_results_added} decisões, {report.feedback_added} respostas")
    print()
    print(f"{'receita':<34} {'acertos':>7} {'erros':>6} {'taxa':>6}  status")
    for recipe in repository.list_recipes():
        tally = report.tallies.get(recipe.id)
        hits = tally.successes if tally else 0
        misses = tally.failures if tally else 0
        line = f"{recipe.id:<34} {hits:>7} {misses:>6} {recipe.expected_rate:>6.2f}"
        print(f"{line}  {recipe.status.value}")


def _cmd_migrate(_: argparse.Namespace) -> None:
    import psycopg

    from prospector.integrations.postgres import migrate

    with psycopg.connect(_database_url()) as conn:
        ran = migrate(conn)
    print("Migrações aplicadas: " + (", ".join(ran) if ran else "nenhuma, banco em dia"))


def _cmd_import_legacy(args: argparse.Namespace) -> None:
    from prospector.integrations.legacy_sqlite import LegacySqliteSource

    source = LegacySqliteSource(args.path)
    weight = float(os.environ.get("LEGACY_PRIOR_WEIGHT", LEGACY_PRIOR_WEIGHT))
    now = datetime.now(UTC)
    cutoff = datetime.combine(args.until, time.max, tzinfo=UTC) if args.until else None

    if args.dry_run:
        from prospector.integrations.memory import InMemoryRepository

        repository = InMemoryRepository()
        report = import_legacy(source, repository, now=now, cutoff=cutoff, weight=weight)
        print("[dry-run] nada foi gravado no banco.\n")
        _print_report(report, repository)
        return

    import psycopg

    from prospector.integrations.postgres import PostgresRepository

    # Um bloco só: a importação inteira entra, ou nada entra.
    with psycopg.connect(_database_url()) as conn:
        postgres = PostgresRepository(conn)
        report = import_legacy(source, postgres, now=now, cutoff=cutoff, weight=weight)
        _print_report(report, postgres)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prospector", description="Prospector Agente")
    commands = parser.add_subparsers(dest="command", required=True)

    db = commands.add_parser("db", help="banco de dados")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("migrate", help="aplica as migrações pendentes").set_defaults(
        handler=_cmd_migrate
    )

    legacy = commands.add_parser("import-legacy", help="importa o histórico do FacebookProspector")
    legacy.add_argument("path", type=Path, help="caminho do prospector.db antigo")
    legacy.add_argument(
        "--until",
        type=date.fromisoformat,
        metavar="AAAA-MM-DD",
        help="importa só o coletado até esse dia (padrão: dia da última resposta)",
    )
    legacy.add_argument(
        "--dry-run", action="store_true", help="simula em memória, sem gravar no banco"
    )
    legacy.set_defaults(handler=_cmd_import_legacy)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)
    args.handler(args)
