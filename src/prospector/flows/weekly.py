"""Os passos do agente semanal que não são LLM.

Ver `docs/arquitetura.md` seção 6. O grafo (`agent/graph.py`) chama estes
casos de uso, e as ferramentas do LLM (`agent/tools.py`) também: as
ferramentas nunca falam com as integrações direto.

O que mora aqui: escolher e enviar a auditoria, decidir se a semana teve
resposta, atualizar o placar, juntar o material que o LLM analisa, testar uma
regra contra o histórico, fazer uma busca de teste, aplicar os ajustes e
montar o resumo.
"""

import random
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from prospector.core.budget import MONTHLY_RESULT_BUDGET, can_spend
from prospector.core.feedback import (
    Feedback,
    FeedbackSource,
    Outcome,
    Verdict,
    post_was_good,
    week_had_feedback,
)
from prospector.core.notification import Notification
from prospector.core.recipe import Recipe, RecipeOrigin, RecipeStatus
from prospector.core.region import HOME_BASE, MAX_DISTANCE_KM
from prospector.core.rules import MAX_POST_AGE, apply_rules
from prospector.core.scoring import ScoringSettings, outcome_of, score_week
from prospector.core.text import strip_accents
from prospector.core.triage import TriageRule
from prospector.core.weekly import (
    AUDIT_SIZE,
    AUDIT_WAIT,
    MAX_RECIPE_PROBES,
    AuditCandidate,
    Change,
    ChangeKind,
    choose_audit,
    rule_change_verdict,
)
from prospector.flows.backtest import BacktestReport, run_backtest
from prospector.flows.ports import (
    JudgeFactory,
    LabeledPost,
    Messenger,
    PostMessage,
    Repository,
    SearchProvider,
    SearchRun,
)

#: O único motivo de ✗ da auditoria: "você descartou, mas era bom".
AUDIT_REJECT_REASONS = ("Era bom, eu chamaria",)

#: Quanto do texto de um post vai para o LLM do agente.
SNIPPET = 300

#: Posts medidos em cada teste de regra. O juiz é barato, mas cada teste roda
#: até este número de chamadas, duas vezes (antes e depois da mudança).
RULE_TEST_LIMIT = 30

#: Resultados de uma busca de teste (`testar_receita`).
PROBE_RESULTS = 5


@dataclass(frozen=True)
class WeeklySettings:
    audit_size: int = AUDIT_SIZE
    audit_wait: timedelta = AUDIT_WAIT
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    monthly_budget: int = MONTHLY_RESULT_BUDGET
    max_probes: int = MAX_RECIPE_PROBES
    probe_results: int = PROBE_RESULTS
    rule_test_limit: int = RULE_TEST_LIMIT
    base: str = HOME_BASE
    max_km: float = MAX_DISTANCE_KM
    max_age: timedelta = MAX_POST_AGE


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= SNIPPET else flat[: SNIPPET - 1] + "…"


# --- auditoria -------------------------------------------------------------


def pick_audit(
    repository: Repository,
    *,
    start: datetime,
    end: datetime,
    rng: random.Random,
    size: int = AUDIT_SIZE,
) -> list[str]:
    """Os descartados da semana que vão para a auditoria."""
    audited = repository.notified_post_ids(FeedbackSource.AUDIT)
    discards = [
        AuditCandidate(item.post_id, item.result.score)
        for item in repository.list_triage_results()
        if not item.result.approved
        and item.decided_at is not None
        and start <= item.decided_at < end
        and item.post_id not in audited
    ]
    return choose_audit(discards, rng=rng, size=size)


@dataclass(frozen=True)
class AuditReport:
    sent: list[Notification] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def send_audit(
    repository: Repository,
    messenger: Messenger,
    post_ids: Sequence[str],
    *,
    now: datetime,
    image_link: Callable[[str], str | None] | None = None,
) -> AuditReport:
    """Manda "descartei, confirma?" para cada post e grava as mensagens.

    A URL de imagem do Facebook pode ter expirado nos dias desde a coleta;
    com a cópia guardada, a prévia do link usa um link temporário dela.
    """
    posts = {post.id: post for post in repository.list_posts()}
    decisions = {item.post_id: item.result for item in repository.list_triage_results()}
    queries = {recipe.id: recipe.query for recipe in repository.list_recipes()}
    report = AuditReport()
    for post_id in post_ids:
        post, result = posts.get(post_id), decisions.get(post_id)
        if post is None or result is None:
            continue
        if image_link is not None and post.image_path:
            link = image_link(post.image_path)
            post = replace(post, image_url=link or post.image_url)
        message = PostMessage(
            post=post,
            result=result,
            queries=tuple(sorted(queries.get(rid, rid) for rid in post.recipe_ids)),
            age=post.age(now),
            reject_reasons=AUDIT_REJECT_REASONS,
            source=FeedbackSource.AUDIT,
        )
        try:
            sent = messenger.send_post(message)
        except Exception as error:
            report.failed.append((post_id, f"{type(error).__name__}: {error}"))
            continue
        report.sent.append(
            Notification(
                chat_id=sent.chat_id,
                message_id=sent.message_id,
                post_id=post_id,
                source=FeedbackSource.AUDIT,
                sent_at=now,
                reject_reasons=AUDIT_REJECT_REASONS,
            )
        )
    repository.add_notifications(report.sent)
    return report


def audit_answered(repository: Repository, post_ids: Sequence[str]) -> bool:
    """Respondi todos os posts da auditoria?"""
    answered = {
        answer.post_id
        for answer in repository.list_feedback()
        if answer.source is FeedbackSource.AUDIT
    }
    return all(post_id in answered for post_id in post_ids)


def had_feedback(repository: Repository, *, start: datetime, end: datetime) -> bool:
    """A semana teve alguma resposta minha? Sem resposta, a semana congela (seção 4)."""
    return week_had_feedback(repository.list_feedback(), start=start, end=end)


# --- placar ----------------------------------------------------------------


@dataclass(frozen=True)
class ScoreReport:
    scored: int = 0
    pending: int = 0
    successes: int = 0
    failures: int = 0
    ignored: int = 0
    retired: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    frozen: int = 0


def _feedback_by_post(repository: Repository) -> dict[str, list[Feedback]]:
    grouped: dict[str, list[Feedback]] = defaultdict(list)
    for answer in repository.list_feedback():
        grouped[answer.post_id].append(answer)
    return grouped


def update_scores(
    repository: Repository,
    *,
    week_id: str,
    now: datetime,
    rng: random.Random,
    settings: ScoringSettings | None = None,
) -> ScoreReport:
    """Esquecimento, posts novos no placar, aposentadoria e promoção.

    Cada post entra uma vez só, quando o resultado dele fica definido. Um
    aprovado ainda no prazo de resposta fica para a semana seguinte.
    """
    answers = _feedback_by_post(repository)
    outcomes = [
        (candidate, outcome_of(candidate, answers.get(candidate.post_id, []), now=now))
        for candidate in repository.list_scoring_candidates()
    ]
    final = [(c, o) for c, o in outcomes if o is not Outcome.PENDING]
    week = score_week(
        repository.list_recipes(),
        [(candidate.recipe_ids, outcome) for candidate, outcome in final],
        rng=rng,
        settings=settings or ScoringSettings(),
    )
    repository.update_recipes(week.recipes)
    repository.mark_scored([(c.post_id, week_id, o.value) for c, o in final])
    return ScoreReport(
        scored=len(final),
        pending=len(outcomes) - len(final),
        successes=sum(o is Outcome.SUCCESS for _, o in final),
        failures=sum(o is Outcome.FAILURE for _, o in final),
        ignored=sum(o is Outcome.IGNORED for _, o in final),
        retired=week.retired,
        promoted=week.promoted,
    )


def freeze_week(repository: Repository, *, week_id: str, now: datetime) -> ScoreReport:
    """Semana sem resposta: os posts dela saem da fila do placar sem contar.

    Sem isso, os descartes da semana congelada seriam cobrados como erro na
    semana seguinte. O aprovado ainda no prazo continua na fila: uma resposta
    atrasada ainda vale.
    """
    answers = _feedback_by_post(repository)
    frozen = [
        candidate.post_id
        for candidate in repository.list_scoring_candidates()
        if outcome_of(candidate, answers.get(candidate.post_id, []), now=now) is not Outcome.PENDING
    ]
    repository.mark_scored([(post_id, week_id, "frozen") for post_id in frozen])
    return ScoreReport(frozen=len(frozen))


# --- o que o LLM analisa ---------------------------------------------------


@dataclass(frozen=True)
class ReviewItem:
    """Uma resposta minha da semana, com o que a triagem tinha dito."""

    post_id: str
    question: str
    """`aprovado` (notificação) ou `descartado` (auditoria)."""

    agent_was_right: bool
    my_reason: str
    triage_reason: str
    snippet: str


def week_review(repository: Repository, *, start: datetime, end: datetime) -> list[ReviewItem]:
    """Minhas respostas da semana, para `analisar_motivos`."""
    posts = {post.id: post for post in repository.list_posts()}
    decisions = {item.post_id: item.result for item in repository.list_triage_results()}
    items = []
    for answer in sorted(repository.list_feedback(), key=lambda a: a.answered_at):
        if not start <= answer.answered_at < end:
            continue
        post, decision = posts.get(answer.post_id), decisions.get(answer.post_id)
        if post is None or decision is None:
            continue
        items.append(
            ReviewItem(
                post_id=answer.post_id,
                question="aprovado"
                if answer.source is FeedbackSource.NOTIFICATION
                else "descartado",
                agent_was_right=answer.verdict is Verdict.RIGHT,
                my_reason=answer.reason or "",
                triage_reason=decision.reason,
                snippet=_snippet(post.text) or "(só imagem)",
            )
        )
    return items


# --- consultas das ferramentas ---------------------------------------------


@dataclass(frozen=True)
class HistoryItem:
    post_id: str
    answered_at: datetime
    good: bool
    """O post era bom (eu teria chamado)?"""

    approved: bool
    my_reason: str
    triage_reason: str
    recipe_ids: frozenset[str]
    snippet: str


def search_history(
    repository: Repository,
    *,
    now: datetime,
    days: int | None = None,
    recipe_id: str | None = None,
    text: str | None = None,
    good: bool | None = None,
    limit: int = 15,
) -> list[HistoryItem]:
    """Posts rotulados por mim, filtrados. Os mais recentes primeiro.

    `text` procura no meu motivo, no motivo da triagem e no texto do post.
    """
    needle = strip_accents((text or "").casefold())
    found = []
    for item in repository.list_labeled_posts():
        is_good = post_was_good(approved=item.decision.approved, feedback=item.feedback)
        if days is not None and item.feedback.answered_at < now - timedelta(days=days):
            continue
        if recipe_id and recipe_id not in item.post.recipe_ids:
            continue
        if good is not None and is_good != good:
            continue
        haystack = " ".join(
            [item.feedback.reason or "", item.decision.reason, item.post.text]
        ).casefold()
        if needle and needle not in strip_accents(haystack):
            continue
        found.append(
            HistoryItem(
                post_id=item.post.id,
                answered_at=item.feedback.answered_at,
                good=is_good,
                approved=item.decision.approved,
                my_reason=item.feedback.reason or "",
                triage_reason=item.decision.reason,
                recipe_ids=item.post.recipe_ids,
                snippet=_snippet(item.post.text) or "(só imagem)",
            )
        )
    found.sort(key=lambda item: item.answered_at, reverse=True)
    return found[: max(1, min(limit, 30))]


# --- testar regra (backtest) -----------------------------------------------


@dataclass(frozen=True)
class RuleComparison:
    """O efeito de uma mudança de regras, medido contra as minhas respostas."""

    before_accuracy: float | None
    after_accuracy: float | None
    before_precision: float | None
    after_precision: float | None
    before_recall: float | None
    after_recall: float | None
    good_posts_lost: int
    """Posts bons que as regras de antes aprovavam e as novas deixariam de aprovar."""

    measured: int
    rejection: str | None
    """`None` se a mudança passa nos guardrails; senão, o porquê."""


class RuleTester:
    """Roda o backtest do juiz com um conjunto de regras, e guarda o resultado.

    O mesmo conjunto de regras não é medido duas vezes na mesma semana: o LLM
    testa uma regra com a ferramenta, e o `validar_ajustes` reaproveita a
    medição em vez de pagar de novo.
    """

    def __init__(
        self,
        labeled: Sequence[LabeledPost],
        make_judge: JudgeFactory,
        *,
        limit: int = RULE_TEST_LIMIT,
        image_link: Callable[[str], str | None] | None = None,
    ) -> None:
        self._labeled = list(labeled)
        self._make_judge = make_judge
        self._limit = limit
        self._image_link = image_link
        self._cache: dict[tuple[str, ...], BacktestReport] = {}

    @staticmethod
    def _key(rules: Sequence[TriageRule]) -> tuple[str, ...]:
        return tuple(sorted(rule.text.strip() for rule in rules if rule.active))

    def measure(self, rules: Sequence[TriageRule]) -> BacktestReport:
        key = self._key(rules)
        if key not in self._cache:
            active = [rule for rule in rules if rule.active]
            self._cache[key] = run_backtest(
                self._labeled,
                lambda examples: self._make_judge(active, examples),
                image_link=self._image_link,
                limit=self._limit,
            )
        return self._cache[key]

    def compare(self, before: Sequence[TriageRule], after: Sequence[TriageRule]) -> RuleComparison:
        old, new = self.measure(before), self.measure(after)
        approved_after = {case.post.id for case in new.cases if case.approved}
        lost = sum(
            1
            for case in old.cases
            if case.good and case.approved and case.post.id not in approved_after
        )
        return RuleComparison(
            before_accuracy=old.combined.accuracy,
            after_accuracy=new.combined.accuracy,
            before_precision=old.combined.precision,
            after_precision=new.combined.precision,
            before_recall=old.combined.recall,
            after_recall=new.combined.recall,
            good_posts_lost=lost,
            measured=len(new.cases),
            rejection=rule_change_verdict(
                baseline_accuracy=old.combined.accuracy,
                proposed_accuracy=new.combined.accuracy,
                good_posts_lost=lost,
            ),
        )


def edit_rules(
    rules: Sequence[TriageRule], *, add: Sequence[str] = (), deactivate: Sequence[str] = ()
) -> list[TriageRule]:
    """As regras como ficariam com a mudança. As novas ganham id provisório."""
    off = set(deactivate)
    edited = [replace(rule, active=False) if rule.id in off else rule for rule in rules]
    edited += [
        TriageRule(id=f"proposta-{index}", text=text.strip(), created_by="agent")
        for index, text in enumerate(add, start=1)
        if text.strip()
    ]
    return edited


# --- testar receita (busca de teste) ---------------------------------------


@dataclass(frozen=True)
class ProbeReport:
    query: str
    charged: int = 0
    returned: int = 0
    kept: int = 0
    """Quantos passaram nas regras fixas (idade, veículo, região, venda...)."""

    snippets: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def probe_query(
    repository: Repository,
    search: SearchProvider,
    query: str,
    *,
    now: datetime,
    settings: WeeklySettings = WeeklySettings(),
) -> ProbeReport:
    """Uma busca de teste com poucos resultados, para ver se o termo traz algo.

    Sai do mesmo orçamento da coleta: a trava é verificada antes, e o gasto é
    gravado em `search_runs` (sem receita, porque o termo ainda não é uma).
    """
    amount = settings.probe_results
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    spent = repository.results_spent_since(month_start)
    if not can_spend(amount, spent, monthly_budget=settings.monthly_budget):
        return ProbeReport(query, skipped_reason="sem orçamento no mês")
    result = search.search(query, {}, limit=amount, since=now - settings.max_age)
    repository.add_search_runs([SearchRun(now, None, amount, result.charged, live=True)])
    kept = [
        post
        for post in result.posts
        if apply_rules(
            post, now=now, base=settings.base, max_km=settings.max_km, max_age=settings.max_age
        ).keep
    ]
    return ProbeReport(
        query=query,
        charged=result.charged,
        returned=len(result.posts),
        kept=len(kept),
        snippets=[_snippet(post.text) or "(só imagem)" for post in kept],
    )


# --- aplicar ajustes -------------------------------------------------------


@dataclass(frozen=True)
class Adjustment:
    """Um ajuste já aprovado pelos guardrails, pronto para gravar."""

    kind: ChangeKind
    reason: str
    text: str = ""
    """Texto da regra nova (`ADD_RULE`)."""

    rule_id: str = ""
    """Regra a desligar (`DEACTIVATE_RULE`)."""

    query: str = ""
    """Termo da receita nova (`ADD_RECIPE`)."""

    parent_id: str | None = None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", strip_accents(text.casefold())).strip("-")[:50] or "x"


def apply_adjustments(
    repository: Repository, adjustments: Sequence[Adjustment], *, week_id: str, now: datetime
) -> list[Change]:
    """Grava regras e receitas, e registra cada ajuste no changelog."""
    rule_texts = {rule.id: rule.text for rule in repository.list_triage_rules()}
    rule_ids = set(rule_texts)
    recipes = {recipe.id: recipe for recipe in repository.list_recipes()}
    changes: list[Change] = []
    for adjustment in adjustments:
        if adjustment.kind is ChangeKind.ADD_RULE:
            number = 1
            while f"agent-{week_id}-{number:02d}" in rule_ids:
                number += 1
            rule_id = f"agent-{week_id}-{number:02d}"
            rule_ids.add(rule_id)
            repository.add_triage_rules([TriageRule(rule_id, adjustment.text, True, "agent")])
            changes.append(
                Change(week_id, adjustment.kind, rule_id, adjustment.reason, now, adjustment.text)
            )
        elif adjustment.kind is ChangeKind.DEACTIVATE_RULE:
            if repository.set_rule_active(adjustment.rule_id, False):
                changes.append(
                    Change(
                        week_id,
                        adjustment.kind,
                        adjustment.rule_id,
                        adjustment.reason,
                        now,
                        rule_texts.get(adjustment.rule_id, ""),
                    )
                )
        elif adjustment.kind is ChangeKind.ADD_RECIPE:
            recipe_id = f"agent-{slug(adjustment.query)}"
            while recipe_id in recipes:
                recipe_id += "-2"
            parent = recipes.get(adjustment.parent_id or "")
            recipe = Recipe(
                id=recipe_id,
                query=adjustment.query.strip(),
                params=dict(parent.params) if parent else {},
                status=RecipeStatus.CANDIDATE,
                origin=RecipeOrigin.AGENT,
                parent_id=parent.id if parent else None,
            )
            recipes[recipe_id] = recipe
            repository.add_recipes([recipe])
            changes.append(
                Change(week_id, adjustment.kind, recipe_id, adjustment.reason, now, recipe.query)
            )
    repository.add_changes(changes)
    return changes


# --- resumo ----------------------------------------------------------------


@dataclass(frozen=True)
class WeekNumbers:
    decided: int = 0
    approved: int = 0
    right: int = 0
    """Respostas a aprovados: "certo"."""

    wrong: int = 0
    audit_right: int = 0
    """Auditoria: "descarte certo"."""

    audit_wrong: int = 0
    """Auditoria: "era bom" (erro do juiz)."""


def week_numbers(repository: Repository, *, start: datetime, end: datetime) -> WeekNumbers:
    decided = [
        item
        for item in repository.list_triage_results()
        if item.decided_at is not None and start <= item.decided_at < end
    ]
    answers = [a for a in repository.list_feedback() if start <= a.answered_at < end]

    def count(source: FeedbackSource, verdict: Verdict) -> int:
        return sum(a.source is source and a.verdict is verdict for a in answers)

    return WeekNumbers(
        decided=len(decided),
        approved=sum(item.result.approved for item in decided),
        right=count(FeedbackSource.NOTIFICATION, Verdict.RIGHT),
        wrong=count(FeedbackSource.NOTIFICATION, Verdict.WRONG),
        audit_right=count(FeedbackSource.AUDIT, Verdict.RIGHT),
        audit_wrong=count(FeedbackSource.AUDIT, Verdict.WRONG),
    )


_CHANGE_LABEL = {
    ChangeKind.ADD_RULE: "Regra nova",
    ChangeKind.DEACTIVATE_RULE: "Regra desligada",
    ChangeKind.ADD_RECIPE: "Busca nova (candidata)",
}


def build_summary(
    *,
    week_id: str,
    numbers: WeekNumbers,
    frozen: bool,
    silent_weeks: int,
    score: ScoreReport,
    changes: Sequence[Change],
    rejected: Sequence[str] = (),
    analysis: str = "",
    agent_error: str | None = None,
    budget_cut_weeks: int = 4,
    reminder_weeks: int = 2,
) -> str:
    """O texto do resumo da semana, que vai para o celular."""
    lines = [f"Resumo da semana ({week_id})", ""]
    lines.append(f"Posts novos triados: {numbers.decided} · aprovados: {numbers.approved}")
    lines.append(f"Suas respostas: {numbers.right} ✓ e {numbers.wrong} ✗ nos aprovados")
    if numbers.audit_right or numbers.audit_wrong:
        lines.append(
            f"Auditoria: {numbers.audit_right} descarte(s) certo(s), "
            f"{numbers.audit_wrong} era(m) bom(ns)"
        )

    if frozen:
        lines += ["", "Sem feedback esta semana, nada mudou."]
        if silent_weeks >= budget_cut_weeks:
            lines.append(
                f"Já são {silent_weeks} semanas sem resposta: a coleta está com metade do "
                "orçamento até você responder alguma coisa."
            )
        elif silent_weeks >= reminder_weeks:
            lines.append(
                f"Lembrete: {silent_weeks} semanas sem resposta. Um ✓ ou ✗ já destrava o "
                "aprendizado."
            )
        return "\n".join(lines)

    lines += ["", f"Placar: {score.scored} posts contados ({score.successes} acertos, "]
    lines[-1] += f"{score.failures} erros, {score.ignored} sem resposta no prazo)"
    if score.retired:
        lines.append("Buscas aposentadas: " + ", ".join(score.retired))
    if score.promoted:
        lines.append("Buscas promovidas a ativas: " + ", ".join(score.promoted))

    if analysis:
        lines += ["", f"O que vi nos seus motivos: {analysis}"]
    if agent_error:
        lines += ["", f"O agente não conseguiu propor ajustes: {agent_error}"]

    if changes:
        lines += ["", "Ajustes aplicados:"]
        for change in changes:
            detail = f': "{change.detail}"' if change.detail else f" ({change.target_id})"
            lines.append(f"- {_CHANGE_LABEL[change.kind]}{detail}")
            lines.append(f"  por quê: {change.reason}")
    else:
        lines += ["", "Nenhum ajuste aplicado esta semana."]
    if rejected:
        lines += ["", "Propostas barradas pelos guardrails:"]
        lines += [f"- {item}" for item in rejected]
    return "\n".join(lines)
