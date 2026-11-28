"""A semana do agente: auditoria, pausa, ajustes e os guardrails que os limitam.

Ver `docs/arquitetura.md` seção 6. O LLM **propõe**; o código daqui **decide**.
Nada neste módulo chama LLM ou banco: são as regras, com testes.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

#: Descartados que vão para a auditoria a cada semana.
AUDIT_SIZE = 2

#: Quanto o grafo espera minhas respostas da auditoria antes de seguir sozinho.
#: Domingo 10h + 22h = segunda 8h (seção 6).
AUDIT_WAIT = timedelta(hours=22)

#: Por semana, no máximo (seção 6, guardrails).
MAX_RULE_CHANGES = 3
MAX_NEW_RECIPES = 1

#: Uma regra nova não pode fazer o juiz jogar fora mais que isso de posts que
#: eu marquei como bons.
MAX_GOOD_POSTS_LOST = 1

#: Buscas de teste no Apify por semana (`testar_receita`).
MAX_RECIPE_PROBES = 2

#: Semanas sem resposta a partir das quais o resumo traz um lembrete.
SILENT_WEEKS_REMINDER = 2


class RunStatus(StrEnum):
    WAITING = "waiting"
    """Auditoria enviada; o grafo está pausado esperando minhas respostas."""

    DONE = "done"


@dataclass(frozen=True)
class WeeklyRun:
    """Uma execução do agente semanal. `week_id` é também o `thread_id` do grafo."""

    week_id: str
    started_at: datetime
    resume_after: datetime
    status: RunStatus = RunStatus.WAITING
    finished_at: datetime | None = None
    summary_chat_id: int | None = None
    summary_message_id: int | None = None
    undone_at: datetime | None = None


def week_id_for(now: datetime) -> str:
    """O nome da semana: a data da execução (o domingo), ex. `2026-11-29`."""
    return now.date().isoformat()


@dataclass(frozen=True)
class AuditCandidate:
    post_id: str
    score: float
    """Nota da triagem. Zero quando as regras fixas barraram (o juiz nem viu)."""


def choose_audit(
    discards: Sequence[AuditCandidate], *, rng: random.Random, size: int = AUDIT_SIZE
) -> list[str]:
    """Sorteia os descartados da auditoria, preferindo os que o juiz chegou a ver.

    Um descarte das regras fixas é quase sempre lixo claro (post velho,
    carreta). O que ensina é o descarte do juiz, porque são as regras dele que o
    agente ajusta. Por isso o sorteio começa pelos posts com nota acima de
    zero, e só completa com os outros se faltar.
    """
    judged = [item.post_id for item in discards if item.score > 0]
    others = [item.post_id for item in discards if item.score <= 0]
    rng.shuffle(judged)
    rng.shuffle(others)
    return (judged + others)[:size]


def should_resume(*, audit_answered: bool, now: datetime, resume_after: datetime) -> bool:
    """O grafo pausado pode seguir? Quando respondi a auditoria ou o prazo venceu."""
    return audit_answered or now >= resume_after


class ChangeKind(StrEnum):
    """Os únicos ajustes que o agente pode fazer. Placar e orçamento não estão aqui."""

    ADD_RULE = "add_rule"
    DEACTIVATE_RULE = "deactivate_rule"
    ADD_RECIPE = "add_recipe"


@dataclass(frozen=True)
class Change:
    """Um ajuste aplicado, com o motivo. É o que o botão Desfazer reverte."""

    week_id: str
    kind: ChangeKind
    target_id: str
    reason: str
    applied_at: datetime
    detail: str = ""
    """O texto da regra ou o termo da receita, para o resumo e para auditar."""

    id: int | None = None
    reverted_at: datetime | None = None


def rule_change_verdict(
    *,
    baseline_accuracy: float | None,
    proposed_accuracy: float | None,
    good_posts_lost: int,
    max_good_lost: int = MAX_GOOD_POSTS_LOST,
) -> str | None:
    """Uma mudança de regra pode entrar? `None` se pode; senão, o porquê em português.

    Regra de triagem só entra se o backtest não piorar a taxa de acerto nem
    descartar mais de `max_good_lost` posts que eu marquei como bons.
    """
    if baseline_accuracy is None or proposed_accuracy is None:
        return "backtest sem posts para medir"
    if proposed_accuracy < baseline_accuracy:
        return f"piorou o acerto ({baseline_accuracy:.0%} → {proposed_accuracy:.0%})"
    if good_posts_lost > max_good_lost:
        return f"descartaria {good_posts_lost} posts que você marcou como bons"
    return None
