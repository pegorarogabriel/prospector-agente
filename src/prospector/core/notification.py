"""A pergunta que vai pro celular e como a minha resposta vira `Feedback`.

Ver `docs/arquitetura.md` seção 8. Aqui não há Telegram: só as regras de
negócio, testáveis sem rede. A integração traduz botão e reply para `Reply`, e
este módulo decide o que cada um significa.

Cada botão carrega uma escolha curta (cabe nos 64 bytes do `callback_data`):

- `r`: ✓ certo;
- `w0`, `w1`, `w2`: ✗ com o motivo de índice 0, 1 ou 2 da lista que foi oferecida
  naquela mensagem (a lista fica gravada junto da notificação);
- `o`: ✗ outro. Não decide nada sozinho: o motivo vem num reply em texto.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from prospector.core.feedback import Feedback, FeedbackSource, Verdict

RIGHT_CHOICE = "r"
OTHER_CHOICE = "o"
DONE_CHOICE = "done"
"""O botão inerte que substitui os outros depois que a resposta é gravada."""

UNDO_CHOICE = "undo"
"""O botão "Desfazer ajustes" do resumo da semana (seção 6, etapa 7)."""

UNDONE_LABEL = "Ajustes da semana desfeitos"

#: Quantos motivos de ✗ viram botão (fora o "Outro").
REJECT_BUTTONS = 3

#: De quanto tempo para trás contam os motivos mais frequentes.
REASON_WINDOW = timedelta(weeks=4)

#: Completam os botões enquanto não houver motivos meus suficientes na janela.
DEFAULT_REJECT_REASONS = ("Fora da região", "Não é oportunidade", "Veículo não serve")

#: O motivo cabe num botão de celular. O que passar disso é cortado no botão,
#: mas o motivo inteiro é o que vai para o banco.
MAX_REASON_LENGTH = 200


def reject_choice(index: int) -> str:
    return f"w{index}"


@dataclass(frozen=True)
class Notification:
    """Uma mensagem que o bot mandou, amarrada ao post e à pergunta feita."""

    chat_id: int
    message_id: int
    post_id: str
    source: FeedbackSource
    sent_at: datetime
    reject_reasons: tuple[str, ...]
    """Os motivos de ✗ oferecidos nesta mensagem, na ordem dos botões."""


@dataclass(frozen=True)
class Reply:
    """Um toque num botão ou um reply em texto a uma mensagem do bot."""

    update_id: int
    chat_id: int
    message_id: int
    """A mensagem do bot que está sendo respondida."""

    answered_at: datetime
    choice: str | None = None
    text: str | None = None


def top_reject_reasons(
    feedback: Iterable[Feedback],
    *,
    now: datetime,
    window: timedelta = REASON_WINDOW,
    size: int = REJECT_BUTTONS,
) -> tuple[str, ...]:
    """Os motivos de ✗ mais frequentes das últimas semanas, para virar botão.

    Conta só respostas a posts aprovados (a pergunta é a mesma da mensagem).
    Motivos iguais a menos de maiúsculas e espaços contam juntos; o texto que
    aparece é o da primeira vez. Os padrões completam a lista sem repetir.
    """
    counts: Counter[str] = Counter()
    spelling: dict[str, str] = {}
    for answer in sorted(feedback, key=lambda item: item.answered_at):
        if (
            answer.verdict is not Verdict.WRONG
            or answer.source is not FeedbackSource.NOTIFICATION
            or not answer.reason
            or answer.answered_at < now - window
        ):
            continue
        key = " ".join(answer.reason.casefold().split())
        spelling.setdefault(key, answer.reason.strip())
        counts[key] += 1

    # Empate: o que apareceu primeiro fica na frente (Counter mantém a ordem).
    chosen = [spelling[key] for key, _ in counts.most_common(size)]
    taken = {" ".join(reason.casefold().split()) for reason in chosen}
    for default in DEFAULT_REJECT_REASONS:
        if len(chosen) == size:
            break
        if default.casefold() not in taken:
            chosen.append(default)
    return tuple(chosen)


def feedback_from_replies(notification: Notification, replies: Sequence[Reply]) -> Feedback | None:
    """Junta as respostas a uma mensagem numa decisão só.

    - O **último botão** decide o veredito: se eu toquei errado e corrigi, vale
      a correção.
    - Um **reply em texto** é o motivo. Sem botão, ele quer dizer ✗ (é o
      caminho do "Outro"). Com ✓, vira o motivo do acerto ("rota boa").
    - "Outro" sem texto ainda não decide nada: espera o reply.
    """
    ordered = sorted(replies, key=lambda reply: reply.update_id)
    buttons = [reply.choice for reply in ordered if reply.choice and reply.choice != DONE_CHOICE]
    texts = [reply.text.strip() for reply in ordered if reply.text and reply.text.strip()]
    last_button = buttons[-1] if buttons else None
    text = texts[-1][:MAX_REASON_LENGTH] if texts else None
    when = ordered[-1].answered_at if ordered else None

    if last_button == RIGHT_CHOICE:
        verdict, reason = Verdict.RIGHT, text
    elif last_button and last_button.startswith("w") and last_button[1:].isdigit():
        index = int(last_button[1:])
        if index >= len(notification.reject_reasons):
            return None
        verdict, reason = Verdict.WRONG, text or notification.reject_reasons[index]
    elif text:
        verdict, reason = Verdict.WRONG, text
    else:
        return None

    assert when is not None
    return Feedback(
        post_id=notification.post_id,
        verdict=verdict,
        source=notification.source,
        answered_at=when,
        reason=reason,
    )


def answer_label(feedback: Feedback) -> str:
    """O texto do botão inerte que confirma o que foi gravado."""
    mark = "✓" if feedback.verdict is Verdict.RIGHT else "✗"
    reason = feedback.reason or ("Certo" if feedback.verdict is Verdict.RIGHT else "Errado")
    return f"Registrado: {mark} {reason}"[:60]
