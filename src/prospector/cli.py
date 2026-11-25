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
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from prospector.flows.collect import CollectReport, CollectSettings
from prospector.flows.import_legacy import LEGACY_PRIOR_WEIGHT, ImportReport, import_legacy
from prospector.flows.ports import Repository

if TYPE_CHECKING:
    from prospector.core.triage import FewShotExample
    from prospector.flows.backtest import BacktestReport
    from prospector.flows.ports import Judge, SearchProvider
    from prospector.integrations.llm import ClaudeJudge


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


def _apify_search() -> "SearchProvider":
    from prospector.integrations.apify import ApifySearchProvider

    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        sys.exit("APIFY_TOKEN não configurado no .env.")
    actor = os.environ.get("APIFY_ACTOR_ID", "").strip() or "scraper_one/facebook-posts-search"
    return ApifySearchProvider(token, actor)


def _claude_judge(repository: Repository, examples: "list[FewShotExample]") -> "ClaudeJudge":
    """O juiz real, com as regras do banco e os exemplos escolhidos."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        sys.exit("ANTHROPIC_API_KEY não configurada no .env.")
    import anthropic

    from prospector.integrations.llm import DEFAULT_MODEL, ClaudeJudge

    model = os.environ.get("JUDGE_MODEL", "").strip() or DEFAULT_MODEL
    return ClaudeJudge(
        client=anthropic.Anthropic(),
        rules=repository.list_triage_rules(),
        examples=examples,
        model=model,
    )


def _print_usage(judge: object) -> None:
    usage = getattr(judge, "usage", None)
    if usage is None or not usage.calls:
        return
    cost = usage.cost_usd(judge.model)
    price = f"US$ {cost:.4f}" if cost is not None else "preço desconhecido"
    print(
        f"Juiz: {usage.calls} chamadas, {usage.input_tokens} tokens de entrada, "
        f"{usage.cache_read_tokens} lidos do cache, {usage.output_tokens} de saída ({price})"
    )


def _cmd_collect(args: argparse.Namespace) -> None:
    if args.live and not args.dry_run:
        sys.exit("--live só faz sentido junto com --dry-run.")
    if not 1 <= args.limit <= MAX_LIVE_TEST_RESULTS:
        sys.exit(f"--limit precisa estar entre 1 e {MAX_LIVE_TEST_RESULTS}.")

    import random

    import psycopg

    from prospector.flows.backtest import choose_examples
    from prospector.flows.collect import collect
    from prospector.flows.ports import Judge, SearchProvider
    from prospector.integrations.fakes import FakeJudge, SampleSearchProvider
    from prospector.integrations.postgres import PostgresRepository

    now = datetime.now(UTC)
    real_search = not args.dry_run or args.live
    if not args.dry_run:
        print("[coleta real] Apify + juiz real; posts, decisões e gasto são gravados.\n")
    elif args.live:
        print(f"[dry-run --live] Apify e juiz de verdade, até {args.limit} resultados.\n")
    else:
        print("[dry-run] posts de exemplo e juiz falso; nada é gravado.\n")

    # Tudo num bloco: se algo falhar no meio, nada da coleta fica pela metade.
    with psycopg.connect(_database_url()) as conn:
        repository = PostgresRepository(conn)
        queries = {recipe.id: recipe.query for recipe in repository.list_recipes()}
        search: SearchProvider = _apify_search() if real_search else SampleSearchProvider(now=now)
        judge: Judge = (
            _claude_judge(repository, choose_examples(repository.list_labeled_posts()))
            if real_search
            else FakeJudge()
        )
        report = collect(
            repository,
            search,
            judge,
            now=now,
            rng=random.Random(),
            settings=_collect_settings(),
            record_spend=real_search,
            write=not args.dry_run,
            test_limit=args.limit if args.live else None,
        )
    _print_collect(report, queries)
    _print_usage(judge)


def _pct(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "-"


def _print_backtest(report: "BacktestReport") -> None:
    good = sum(example.good for example in report.examples)
    print(
        f"Exemplos no prompt: {good} bons e {len(report.examples) - good} ruins (fora da medição)"
    )
    print(f"Posts medidos: {len(report.cases)}  ", end="")
    print(f"(só imagem, pulados: {report.skipped_image_only}; ", end="")
    print(f"fora da amostra: {report.skipped_by_limit})\n")
    print(f"{'':<14}{'acerto':>8}{'precisão':>10}{'recall':>8}    VP  FP  FN  VN")
    for label, confusion in (
        ("regras", report.rules),
        ("juiz", report.judge),
        ("duas camadas", report.combined),
    ):
        print(
            f"{label:<14}{_pct(confusion.accuracy):>8}{_pct(confusion.precision):>10}"
            f"{_pct(confusion.recall):>8}   {confusion.true_positive:>3} "
            f"{confusion.false_positive:>3} {confusion.false_negative:>3} "
            f"{confusion.true_negative:>3}"
        )
    print("\nprecisão: do que foi aprovado, quanto era bom (baixa = celular cheio de lixo)")
    print("recall: do que era bom, quanto foi aprovado (baixo = oportunidade perdida)")

    if report.rule_errors:
        print("\nPosts bons que as regras jogaram fora:")
        for case in report.rule_errors:
            snippet = case.post.text.replace("\n", " ")[:70]
            print(f"  - {case.rules.reason} | {snippet}")
    if report.judge_errors:
        print("\nErros do juiz:")
        for case in report.judge_errors:
            kind = "aprovou, mas você não chamou" if case.approved else "reprovou, mas você chamou"
            snippet = case.post.text.replace("\n", " ")[:70]
            print(f"  - {kind}: {snippet}")
            print(f"      juiz: {case.judged.reason}")
            if case.my_reason:
                print(f"      você: {case.my_reason}")


def _cmd_backtest(args: argparse.Namespace) -> None:
    import psycopg

    from prospector.flows.backtest import run_backtest
    from prospector.integrations.fakes import FakeJudge
    from prospector.integrations.postgres import PostgresRepository

    if args.dry_run:
        print("[dry-run] juiz falso: testa o encanamento, sem custo.\n")
    built: list[object] = []
    with psycopg.connect(_database_url()) as conn:
        repository = PostgresRepository(conn)

        def make_judge(examples: "list[FewShotExample]") -> "Judge":
            judge = FakeJudge() if args.dry_run else _claude_judge(repository, examples)
            built.append(judge)
            return judge

        report = run_backtest(repository.list_labeled_posts(), make_judge, limit=args.limit)
    _print_backtest(report)
    print()
    if built:
        _print_usage(built[0])


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
        help="com --dry-run: Apify e juiz de verdade (gasta crédito; grava só o gasto)",
    )
    collect.add_argument(
        "--limit",
        type=int,
        default=5,
        help=f"resultados do teste --live (1 a {MAX_LIVE_TEST_RESULTS}, padrão 5)",
    )
    collect.set_defaults(handler=_cmd_collect)

    backtest = commands.add_parser("backtest", help="mede o juiz contra as minhas respostas")
    backtest.add_argument(
        "--dry-run", action="store_true", help="juiz falso: testa o encanamento sem custo"
    )
    backtest.add_argument("--limit", type=int, default=None, help="mede só N posts")
    backtest.set_defaults(handler=_cmd_backtest)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)
    args.handler(args)
