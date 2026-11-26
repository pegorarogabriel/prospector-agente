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
    from prospector.flows.ingest_replies import IngestReport
    from prospector.flows.notify import NotifyReport
    from prospector.flows.ports import Judge, PostMessage, SearchProvider
    from prospector.integrations.llm import ClaudeJudge
    from prospector.integrations.supabase_storage import SupabaseImageStore
    from prospector.integrations.telegram import TelegramMessenger


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


def _print_collect(report: CollectReport, queries: dict[str, str], *, summary: bool) -> None:
    """Relatório da coleta. Com `summary`, só números: nada de texto de post.

    Log do GitHub Actions em repositório público é público, e os posts têm
    nome e telefone de gente real.
    """
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
    judged = [d for d in report.decisions if d.judged]
    print(f"\nPosts novos: {len(report.decisions)}  ", end="")
    print(f"(barrados pelas regras: {len(report.decisions) - len(judged)}, ", end="")
    print(f"julgados: {len(judged)}, aprovados: {len(approved)})")
    if not summary:
        for decision in dropped:
            print(f"  ✗ {decision.post.id:<20} {decision.result.reason}")
        notify = {d.post.id for d in report.to_notify}
        for decision in sorted(approved, key=lambda d: d.result.score, reverse=True):
            mark = "→ Telegram" if decision.post.id in notify else ""
            kind = "imagem" if decision.post.image_url else "texto"
            snippet = (decision.post.text or "(só imagem)").replace("\n", " ")[:50]
            line = f"{decision.post.id:<20} {decision.result.score:.2f} [{kind}] {snippet}"
            print(f"  ✓ {line} {mark}")

    print()
    print("Gasto gravado em search_runs." if report.recorded_spend else "Nenhum gasto gravado.")
    print("Posts e decisões gravados." if report.wrote else "Nenhum post ou decisão gravado.")


def _telegram() -> "TelegramMessenger":
    from prospector.integrations.telegram import TelegramMessenger

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN não configurado no .env.")
    if not chat.lstrip("-").isdigit():
        sys.exit("TELEGRAM_CHAT_ID não configurado. Rode `prospector telegram chats` para achar.")
    return TelegramMessenger(token, int(chat))


def _image_store() -> "SupabaseImageStore":
    """O Storage das cópias de imagem, com a chave e o bucket já conferidos."""
    from prospector.integrations.supabase_storage import SupabaseImageStore

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        sys.exit("SUPABASE_URL e SUPABASE_SERVICE_KEY precisam estar no .env.")
    if key.startswith("sb_publishable_"):
        sys.exit(
            "SUPABASE_SERVICE_KEY é a chave publicável (sb_publishable_...). Use a secreta "
            "(sb_secret_...), em Project Settings → API Keys."
        )
    store = SupabaseImageStore(url, key)
    problem = store.check()
    if problem:
        sys.exit(f"Storage: {problem}")
    return store


def _print_images(saved: int, failed: int, errors: list[str]) -> None:
    print(f"Imagens copiadas para o Storage: {saved}, sem cópia: {failed}")
    for error, count in sorted({e: errors.count(e) for e in errors}.items()):
        print(f"  {count}x {error}")


def _print_preview(messages: "list[PostMessage]") -> None:
    from prospector.integrations.telegram import render_post

    print(f"\nMensagens que iriam pro Telegram: {len(messages)}")
    for message in messages:
        body = render_post(message)
        print("  " + "-" * 60)
        for line in body["text"].splitlines():
            print(f"  {line}")
        for [button] in body["reply_markup"]["inline_keyboard"]:
            print(f"  [{button['text']}]")


def _print_notify(report: "NotifyReport") -> None:
    print(f"\nTelegram: {len(report.sent)} enviados, {len(report.failed)} com erro")
    for post_id, error in report.failed:
        print(f"  erro no post {post_id}: {error}")


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
    from prospector.flows.notify import build_messages, notify_approved
    from prospector.flows.ports import Judge, SearchProvider
    from prospector.integrations.fakes import FakeJudge, SampleSearchProvider
    from prospector.integrations.postgres import PostgresRepository

    now = datetime.now(UTC)
    real_search = not args.dry_run or args.live
    # Antes de gastar Apify e LLM: coleta real sem Telegram não chegaria em ninguém.
    messenger = _telegram() if not args.dry_run else None
    images = _image_store() if not args.dry_run else None
    if not args.dry_run:
        print("[coleta real] Apify + juiz real; grava tudo e manda os aprovados.\n")
    elif args.live:
        print(f"[dry-run --live] Apify e juiz de verdade, até {args.limit} resultados.\n")
    else:
        print("[dry-run] posts de exemplo e juiz falso; nada é gravado nem enviado.\n")

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
            image_store=images,
        )
        _print_collect(report, queries, summary=args.summary)
        _print_usage(judge)
        if images is not None:
            _print_images(report.images_saved, report.images_failed, images.errors)

        if messenger is None:
            if not args.summary:
                _print_preview(build_messages(repository, report.to_notify, now=now))
            return
        # A coleta fica gravada antes do envio: se o Telegram falhar, os posts
        # e o gasto não se perdem. Cada notificação aponta para um post já salvo.
        conn.commit()
        notified = notify_approved(repository, messenger, report.to_notify, now=now)
    _print_notify(notified)
    if report.to_notify and not notified.sent:
        sys.exit("Nenhum aprovado chegou ao Telegram.")


def _print_ingest(report: "IngestReport", *, write: bool, summary: bool) -> None:
    print(f"Atualizações lidas: {report.replies_read}")
    print(f"  sem notificação conhecida:  {report.unknown_message}")
    print(f"  'Outro' esperando o motivo: {report.waiting_reason}")
    print(f"Respostas novas: {len(report.feedback)}", end="")
    print(f" (gravadas: {report.recorded})" if write else " (dry-run: nada gravado)")
    if not summary:
        for answer in report.feedback:
            mark = "✓" if answer.verdict.value == "right" else "✗"
            print(f"  {mark} {answer.post_id:<20} {answer.reason or ''}")


def _cmd_ingest_replies(args: argparse.Namespace) -> None:
    import psycopg

    from prospector.flows.ingest_replies import confirm_replies, ingest_replies
    from prospector.integrations.postgres import PostgresRepository

    messenger = _telegram()
    if args.dry_run:
        print("[dry-run] lê o Telegram, mas não grava nem confirma nada.\n")
    with psycopg.connect(_database_url()) as conn:
        report = ingest_replies(PostgresRepository(conn), messenger, write=not args.dry_run)
        # O celular só diz "registrado" depois que o banco confirmou.
        conn.commit()
    _print_ingest(report, write=not args.dry_run, summary=args.summary)
    if args.dry_run:
        return
    for error in confirm_replies(messenger, report):
        print(f"  confirmação no celular falhou: {error}")


def _cmd_telegram_chats(_: argparse.Namespace) -> None:
    from prospector.integrations.telegram import TelegramMessenger, chats_from_updates

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN não configurado no .env.")
    # Sem offset: só olha, não confirma nada. As respostas continuam lá para
    # o `ingest-replies`.
    chats = chats_from_updates(TelegramMessenger(token, 0).fetch_updates(None))
    if not chats:
        print("Nenhuma mensagem recente. Mande um 'oi' para o bot e rode de novo.")
        return
    print("Chats que falaram com o bot (coloque o seu em TELEGRAM_CHAT_ID):")
    for chat_id, name in chats.items():
        print(f"  {chat_id}  {name}")


def _cmd_storage_check(_: argparse.Namespace) -> None:
    store = _image_store()
    print("Chave secreta e bucket privado 'post-images': ok")
    problem = store.round_trip()
    if problem:
        sys.exit(f"Teste de gravação falhou: {problem}")
    print("Gravar, gerar link e apagar um arquivo de teste: ok")


def _cmd_telegram_ping(_: argparse.Namespace) -> None:
    _telegram().send_text("Prospector: teste de conexão. Se chegou, o bot está configurado.")
    print("Mensagem de teste enviada.")


def _pct(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "-"


def _print_backtest(report: "BacktestReport") -> None:
    good = sum(example.good for example in report.examples)
    print(
        f"Exemplos no prompt: {good} bons e {len(report.examples) - good} ruins (fora da medição)"
    )
    print(f"Posts medidos: {len(report.cases)}  ", end="")
    print(f"(só imagem sem cópia, pulados: {report.skipped_image_only}; ", end="")
    print(f"fora da amostra: {report.skipped_by_limit}; ", end="")
    print(f"julgados com a cópia da imagem: {report.stored_images})\n")
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
    # Com o Storage configurado, o juiz vê a cópia das imagens; sem ele, post
    # só de imagem é pulado (a URL do Facebook já expirou).
    configured = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    images = _image_store() if configured and not args.dry_run else None
    built: list[object] = []
    with psycopg.connect(_database_url()) as conn:
        repository = PostgresRepository(conn)

        def make_judge(examples: "list[FewShotExample]") -> "Judge":
            judge = FakeJudge() if args.dry_run else _claude_judge(repository, examples)
            built.append(judge)
            return judge

        report = run_backtest(
            repository.list_labeled_posts(),
            make_judge,
            image_link=images.link if images else None,
            limit=args.limit,
        )
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


_SUMMARY_HELP = "só números, sem texto de post (para log público do GitHub Actions)"


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
    collect.add_argument("--summary", action="store_true", help=_SUMMARY_HELP)
    collect.set_defaults(handler=_cmd_collect)

    replies = commands.add_parser("ingest-replies", help="grava meus ✓/✗ do Telegram no banco")
    replies.add_argument(
        "--dry-run", action="store_true", help="lê e mostra, sem gravar nem confirmar"
    )
    replies.add_argument("--summary", action="store_true", help=_SUMMARY_HELP)
    replies.set_defaults(handler=_cmd_ingest_replies)

    telegram = commands.add_parser("telegram", help="configuração do bot")
    telegram_commands = telegram.add_subparsers(dest="telegram_command", required=True)
    telegram_commands.add_parser(
        "chats", help="lista quem falou com o bot (para achar o TELEGRAM_CHAT_ID)"
    ).set_defaults(handler=_cmd_telegram_chats)
    telegram_commands.add_parser("ping", help="manda uma mensagem de teste").set_defaults(
        handler=_cmd_telegram_ping
    )

    storage = commands.add_parser("storage", help="cópia das imagens no Supabase Storage")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_commands.add_parser(
        "check", help="confere a chave e o bucket, e testa gravar, ler e apagar"
    ).set_defaults(handler=_cmd_storage_check)

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
