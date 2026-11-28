"""Lê meus ✓/✗ no Telegram e grava no banco.

Ver `docs/arquitetura.md` seção 8. Roda a cada 2h, sem servidor: pergunta ao
Telegram o que chegou desde a última leitura (`getUpdates`).

O offset (até onde já li) é gravado **na mesma transação** das respostas. O
Telegram só esquece uma atualização quando a próxima leitura pede um offset
depois dela. Então, se a gravação falhar, a próxima rodada lê tudo de novo; e,
como `add_feedback` é "insere se não existe", ler duas vezes não duplica nada.

A confirmação no celular (trocar os botões por "Registrado: ...") fica de fora
desta função de propósito: ela só pode acontecer depois do commit. Senão o
celular diria "registrado" sobre algo que o banco desfez.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from prospector.core.feedback import Feedback
from prospector.core.notification import (
    UNDO_CHOICE,
    UNDONE_LABEL,
    Notification,
    Reply,
    answer_label,
    feedback_from_replies,
)
from prospector.flows.ports import Messenger, Repository
from prospector.flows.undo import undo_week


@dataclass(frozen=True)
class Confirmation:
    chat_id: int
    message_id: int
    label: str


@dataclass(frozen=True)
class IngestReport:
    replies_read: int
    unknown_message: int
    """Respostas a mensagens que não são notificação (um "oi" solto, uma mensagem apagada)."""

    waiting_reason: int
    """Mensagens com "Outro" tocado e ainda sem o motivo em texto."""

    feedback: list[Feedback] = field(default_factory=list)
    """Respostas novas: posts que ainda não tinham resposta para esta pergunta."""

    recorded: int = 0
    """Quantas entraram no banco (zero sem `write`)."""

    confirmations: list[Confirmation] = field(default_factory=list)

    undone_weeks: list[str] = field(default_factory=list)
    """Semanas cujo botão "Desfazer ajustes" eu toquei (desfeitas só com `write`)."""


def ingest_replies(
    repository: Repository,
    messenger: Messenger,
    *,
    write: bool,
    now: datetime | None = None,
) -> IngestReport:
    """Lê, interpreta e (com `write`) grava as respostas e o novo offset.

    Um toque em "Desfazer ajustes" no resumo da semana desfaz os ajustes dela
    na mesma transação das respostas.
    """
    batch = messenger.fetch_replies(repository.reply_offset())

    by_message: dict[tuple[int, int], list[Reply]] = defaultdict(list)
    for reply in batch.replies:
        by_message[(reply.chat_id, reply.message_id)].append(reply)

    known: dict[tuple[int, int], Notification] = {}
    for chat_id in {chat for chat, _ in by_message}:
        ids = [message_id for chat, message_id in by_message if chat == chat_id]
        for message_id, item in repository.find_notifications(chat_id, ids).items():
            known[(chat_id, message_id)] = item

    # Resposta gravada não muda: vale a primeira que chegou ao banco. A
    # confirmação mostra essa, para o celular não dizer outra coisa.
    answered = {
        (answer.post_id, answer.source): answer
        for answer in (repository.list_feedback() if known else [])
    }

    feedback: list[Feedback] = []
    confirmations: list[Confirmation] = []
    undone: list[str] = []
    unknown = waiting = 0
    for key, replies in by_message.items():
        notification = known.get(key)
        if notification is None:
            week = _undo_request(repository, key, replies)
            if week is None:
                unknown += len(replies)
                continue
            undone.append(week)
            if write:
                undo_week(repository, week, now=now or replies[-1].answered_at)
            confirmations.append(Confirmation(key[0], key[1], UNDONE_LABEL))
            continue
        previous = answered.get((notification.post_id, notification.source))
        answer = previous or feedback_from_replies(notification, replies)
        if answer is None:
            waiting += 1
            continue
        if previous is None:
            feedback.append(answer)
        confirmations.append(Confirmation(key[0], key[1], answer_label(answer)))

    recorded = 0
    if write:
        recorded = repository.add_feedback(feedback)
        if batch.next_offset is not None:
            repository.save_reply_offset(batch.next_offset)

    return IngestReport(
        replies_read=len(batch.replies),
        unknown_message=unknown,
        waiting_reason=waiting,
        feedback=feedback,
        recorded=recorded,
        confirmations=confirmations,
        undone_weeks=undone,
    )


def _undo_request(repository: Repository, key: tuple[int, int], replies: list[Reply]) -> str | None:
    """A semana a desfazer, se estas respostas são o botão Desfazer de um resumo."""
    if not any(reply.choice == UNDO_CHOICE for reply in replies):
        return None
    run = repository.find_weekly_run_by_message(*key)
    return run.week_id if run is not None else None


def confirm_replies(messenger: Messenger, report: IngestReport) -> list[str]:
    """Mostra no celular o que foi gravado. Chame depois do commit.

    É cosmético: se falhar, a resposta continua gravada. Devolve os erros.
    """
    errors = []
    for item in report.confirmations:
        try:
            messenger.confirm(item.chat_id, item.message_id, item.label)
        except Exception as error:
            errors.append(f"mensagem {item.message_id}: {type(error).__name__}: {error}")
    return errors
