"""Meu veredito sobre a decisão do agente, e o que ele significa para o placar.

Ver a tabela "O que conta como acerto e erro" em `docs/arquitetura.md` seção 3.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

#: Prazo para eu responder um post aprovado. Depois disso a tentativa é ignorada,
#: em vez de contar como erro — meu silêncio não é informação sobre a receita.
REPLY_DEADLINE = timedelta(days=7)


class Verdict(StrEnum):
    """O que eu disse sobre a DECISÃO do agente — não sobre o post.

    Essa distinção é o que faz a auditoria funcionar. No Telegram eu respondo a
    duas perguntas diferentes: "aprovei este post, certo?" e "descartei este
    post, confirma?". Em ambas, `RIGHT` quer dizer "você decidiu bem".
    """

    RIGHT = "right"
    WRONG = "wrong"


class FeedbackSource(StrEnum):
    """De qual pergunta veio a resposta."""

    NOTIFICATION = "notification"
    """Resposta a um post que o agente aprovou e me mandou."""

    AUDIT = "audit"
    """Resposta à auditoria semanal de um post que o agente descartou."""


class Outcome(StrEnum):
    """O que a tentativa valeu para a receita que trouxe o post."""

    SUCCESS = "success"
    FAILURE = "failure"

    IGNORED = "ignored"
    """Aprovado e sem resposta no prazo. Não entra no placar."""

    PENDING = "pending"
    """Ainda dentro do prazo de resposta. Não entra no placar *ainda*."""


@dataclass(frozen=True)
class Feedback:
    """Minha resposta sobre uma decisão da triagem."""

    post_id: str
    verdict: Verdict
    source: FeedbackSource
    answered_at: datetime
    reason: str | None = None
    """Meu motivo, quando dei um. Vira exemplo few-shot e regra de triagem."""


def recipe_outcome(
    *,
    approved: bool,
    feedback: Feedback | None,
    age: timedelta,
    deadline: timedelta = REPLY_DEADLINE,
) -> Outcome:
    """Traduz o destino de um post no que ele valeu para a receita de origem.

    A regra toda cabe numa linha: **a receita acerta quando o post era bom**, e
    o post era bom exatamente quando `approved` e "o agente decidiu bem"
    concordam. Se o agente aprovou e eu confirmei, era bom. Se ele descartou e
    eu disse que errou, também era bom.

    Um descarte sem auditoria conta como erro mesmo que eu nunca veja o post:
    uma receita que só traz lixo precisa ser punida, e não só aquelas cujos
    posts chegam até mim. É isso que dá dados suficientes para o placar
    convergir (~210 posts por semana, contra os ~10 que eu reviso).
    """
    if feedback is None:
        if not approved:
            return Outcome.FAILURE
        return Outcome.IGNORED if age >= deadline else Outcome.PENDING

    agent_was_right = feedback.verdict is Verdict.RIGHT
    return Outcome.SUCCESS if approved == agent_was_right else Outcome.FAILURE


def is_triage_error(*, feedback: Feedback | None) -> bool:
    """O juiz errou nesta decisão?

    Isso é contado contra a *triagem*, separado do placar da receita: um post
    bom que o juiz descartou é acerto para a receita (ela achou algo bom) e
    erro para o juiz (ele jogou fora). Alimenta `analisar_motivos` no agente
    semanal, que é quem ajusta as regras de triagem.
    """
    return feedback is not None and feedback.verdict is Verdict.WRONG
