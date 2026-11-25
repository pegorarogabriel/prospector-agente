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
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

from prospector.flows.collect import CollectReport, CollectSettings
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


#: Teto de resultados do teste `--live`: ele existe para validar a integração,
#: não para coletar.
MAX_LIVE_TEST_RESULTS = 10


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _collect_settings() -> CollectSettings:
    """Os limites da coleta, lidos do `.env` com os padrões da arquitetura.

    O `core` não lê ambiente: é aqui, na montagem, que a configuração vira
    parâmetro.
    """
    defaults = CollectSettings()
    return CollectSettings(
        monthly_budget=_env_int("MONTHLY_RESULT_BUDGET", defaults.monthly_budget),
        daily_cap=_env_int("MAX_DAILY_RESULTS", defaults.daily_cap),
        max_notifications=_env_int("MAX_DAILY_NOTIFICATIONS", defaults.max_notifications),
        base=os.environ.get("HOME_BASE", "").strip() or defaults.base,
        max_age=timedelta(
            hours=_env_int("MAX_POST_AGE_HOURS", int(defaults.max_age.total_seconds() // 3600))
        ),
    )


def _print_collect(report: CollectReport, queries: dict[str, str]) -> None:
    print(f"Gasto no mês antes desta coleta: {report.spent_before} resultados")
    print(f"Orçamento de hoje: {report.budget} resultados\n")

    print("Busca por receita (sorteio de Thompson):")
    for search in report.searches:
        if search.skipped_reason:
            print(f"  {queries[search.recipe_id]:<28} {search.requested:>3} pedidos  ", end="")
            print(f"BARRADA: {search.skipped_reason}")
            continue
        print(f"  {queries[search.recipe_id]:<28} {search.requested:>3} pedidos  ", end="")
        print(f"{search.charged:>3} cobrados  {search.returned:>3} posts")
    print(f"\nDuplicados entre receitas: {report.duplicates_merged}")
    print(f"Já vistos em coletas anteriores: {report.already_seen}")

    approved = [d for d in report.decisions if d.result.approved]
    dropped = [d for d in report.decisions if not d.result.approved]
    print(f"\nPosts novos: {len(report.decisions)}  ", end="")
    print(f"(descartados pelas regras: {len(dropped)}, aprovados pelo juiz: {len(approved)})")
    for decision in dropped:
        print(f"  ✗ {decision.post.id:<20} {decision.result.reason}")
    notify = {d.post.id for d in report.to_notify}
    for decision in sorted(approved, key=lambda d: d.result.score, reverse=True):
        mark = "→ Telegram" if decision.post.id in notify else ""
        kind = "imagem" if decision.post.image_url else "texto"
        snippet = (decision.post.text or "(só imagem)").replace("\n", " ")[:50]
        print(f"  ✓ {decision.post.id:<20} {decision.result.score:.2f} [{kind}] {snippet} {mark}")

    print()
    print("Gasto gravado em search_runs." if report.recorded_spend else "Nenhum gasto gravado.")
    print("Posts e decisões gravados." if report.wrote else "Nenhum post ou decisão gravado.")


def _cmd_collect(args: argparse.Namespace) -> None:
    if not args.dry_run:
        sys.exit(
            "A coleta de verdade liga quando o juiz real existir (etapa 4). "
            "Por enquanto use --dry-run, ou --dry-run --live para testar o Apify."
        )
    if not 1 <= args.limit <= MAX_LIVE_TEST_RESULTS:
        sys.exit(f"--limit precisa estar entre 1 e {MAX_LIVE_TEST_RESULTS}.")

    import random

    import psycopg

    from prospector.flows.collect import collect
    from prospector.flows.ports import SearchProvider
    from prospector.integrations.fakes import FakeJudge, SampleSearchProvider
    from prospector.integrations.postgres import PostgresRepository

    now = datetime.now(UTC)
    search: SearchProvider
    if args.live:
        from prospector.integrations.apify import ApifySearchProvider

        token = os.environ.get("APIFY_TOKEN", "").strip()
        if not token:
            sys.exit("APIFY_TOKEN não configurado no .env.")
        actor = os.environ.get("APIFY_ACTOR_ID", "").strip() or "scraper_one/facebook-posts-search"
        search = ApifySearchProvider(token, actor)
        print(f"[dry-run --live] buscando no Apify de verdade, até {args.limit} resultados.\n")
    else:
        search = SampleSearchProvider(now=now)
        print("[dry-run] posts de exemplo no lugar do Apify; nada é gravado.\n")

    # Só o teste --live grava algo: o gasto, porque o crédito foi consumido.
    with psycopg.connect(_database_url()) as conn:
        repository = PostgresRepository(conn)
        queries = {recipe.id: recipe.query for recipe in repository.list_recipes()}
        report = collect(
            repository,
            search,
            FakeJudge(),
            now=now,
            rng=random.Random(),
            settings=_collect_settings(),
            record_spend=args.live,
            write=False,
            test_limit=args.limit if args.live else None,
        )
    _print_collect(report, queries)


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

    collect = commands.add_parser("collect", help="coleta e triagem diária")
    collect.add_argument(
        "--dry-run", action="store_true", help="posts de exemplo, juiz falso, nada gravado"
    )
    collect.add_argument(
        "--live",
        action="store_true",
        help="com --dry-run: busca no Apify de verdade (gasta crédito; grava só o gasto)",
    )
    collect.add_argument(
        "--limit",
        type=int,
        default=5,
        help=f"resultados do teste --live (1 a {MAX_LIVE_TEST_RESULTS}, padrão 5)",
    )
    collect.set_defaults(handler=_cmd_collect)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)
    args.handler(args)
