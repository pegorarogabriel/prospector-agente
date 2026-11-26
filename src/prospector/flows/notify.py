"""Manda os aprovados do dia para o celular e lembra de cada mensagem.

Ver `docs/arquitetura.md` seção 8. Roda depois da coleta, com os posts já
gravados: a notificação aponta para o post no banco, e é por ela que um toque
num botão chega ao post certo.

Uma mensagem que falha no envio não derruba as outras: o erro entra no
relatório e o resto segue.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from prospector.core.feedback import FeedbackSource
from prospector.core.notification import Notification, top_reject_reasons
from prospector.flows.collect import Decision
from prospector.flows.ports import Messenger, PostMessage, Repository


@dataclass(frozen=True)
class NotifyReport:
    sent: list[Notification] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    """(post_id, erro) de cada envio que falhou."""


def build_messages(
    repository: Repository, decisions: Sequence[Decision], *, now: datetime
) -> list[PostMessage]:
    """As mensagens dos aprovados, com os botões de ✗ mais usados nas últimas semanas."""
    reasons = top_reject_reasons(repository.list_feedback(), now=now)
    queries = {recipe.id: recipe.query for recipe in repository.list_recipes()}
    return [
        PostMessage(
            post=decision.post,
            result=decision.result,
            queries=tuple(sorted(queries.get(rid, rid) for rid in decision.post.recipe_ids)),
            age=decision.post.age(now),
            reject_reasons=reasons,
        )
        for decision in decisions
    ]


def notify_approved(
    repository: Repository,
    messenger: Messenger,
    decisions: Sequence[Decision],
    *,
    now: datetime,
) -> NotifyReport:
    report = NotifyReport()
    for message in build_messages(repository, decisions, now=now):
        try:
            sent = messenger.send_post(message)
        except Exception as error:
            report.failed.append((message.post.id, f"{type(error).__name__}: {error}"))
            continue
        report.sent.append(
            Notification(
                chat_id=sent.chat_id,
                message_id=sent.message_id,
                post_id=message.post.id,
                source=FeedbackSource.NOTIFICATION,
                sent_at=now,
                reject_reasons=message.reject_reasons,
            )
        )
    repository.add_notifications(report.sent)
    return report
