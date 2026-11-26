"""Como uma resposta no Telegram vira `Feedback` (arquitetura seção 8)."""

from datetime import UTC, datetime, timedelta

from prospector.core.feedback import Feedback, FeedbackSource, Verdict
from prospector.core.notification import (
    DEFAULT_REJECT_REASONS,
    Notification,
    Reply,
    answer_label,
    feedback_from_replies,
    top_reject_reasons,
)

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
NOTE = Notification(
    chat_id=1,
    message_id=100,
    post_id="p1",
    source=FeedbackSource.NOTIFICATION,
    sent_at=NOW,
    reject_reasons=("Fora da região", "Não é oportunidade", "Veículo não serve"),
)


def reply(update_id: int, *, choice: str | None = None, text: str | None = None) -> Reply:
    return Reply(update_id, 1, 100, NOW + timedelta(minutes=update_id), choice, text)


def wrong(reason: str, days_ago: float = 1, source=FeedbackSource.NOTIFICATION) -> Feedback:
    return Feedback("x", Verdict.WRONG, source, NOW - timedelta(days=days_ago), reason)


# --- botões de motivo ------------------------------------------------------


def test_without_history_the_buttons_are_the_defaults():
    assert top_reject_reasons([], now=NOW) == DEFAULT_REJECT_REASONS


def test_most_frequent_reasons_come_first_and_defaults_fill_the_rest():
    feedback = [wrong("mudança"), wrong("Mudança "), wrong("vende caminhão"), wrong("mudança")]
    assert top_reject_reasons(feedback, now=NOW) == (
        "mudança",
        "vende caminhão",
        "Fora da região",
    )


def test_a_default_already_chosen_is_not_repeated():
    feedback = [wrong("fora da região"), wrong("fora da região"), wrong("mudança")]
    assert top_reject_reasons(feedback, now=NOW) == (
        "fora da região",
        "mudança",
        "Não é oportunidade",
    )


def test_old_audit_and_right_answers_do_not_count():
    feedback = [
        wrong("antigo", days_ago=40),
        wrong("da auditoria", source=FeedbackSource.AUDIT),
        Feedback("x", Verdict.RIGHT, FeedbackSource.NOTIFICATION, NOW, "rota boa"),
    ]
    assert top_reject_reasons(feedback, now=NOW) == DEFAULT_REJECT_REASONS


# --- respostas viram feedback -----------------------------------------------


def test_right_button():
    answer = feedback_from_replies(NOTE, [reply(1, choice="r")])
    assert answer == Feedback(
        "p1", Verdict.RIGHT, FeedbackSource.NOTIFICATION, reply(1).answered_at
    )


def test_reject_button_uses_the_reason_offered_in_that_message():
    answer = feedback_from_replies(NOTE, [reply(1, choice="w2")])
    assert answer is not None
    assert (answer.verdict, answer.reason) == (Verdict.WRONG, "Veículo não serve")


def test_the_last_button_wins():
    answer = feedback_from_replies(NOTE, [reply(2, choice="r"), reply(1, choice="w0")])
    assert answer is not None and answer.verdict is Verdict.RIGHT


def test_other_waits_for_the_text_reply():
    assert feedback_from_replies(NOTE, [reply(1, choice="o")]) is None
    answer = feedback_from_replies(NOTE, [reply(1, choice="o"), reply(2, text=" é mudança ")])
    assert answer is not None
    assert (answer.verdict, answer.reason) == (Verdict.WRONG, "é mudança")


def test_text_alone_means_wrong_and_text_with_right_is_the_reason():
    alone = feedback_from_replies(NOTE, [reply(1, text="só tem carreta")])
    assert alone is not None and alone.verdict is Verdict.WRONG
    praised = feedback_from_replies(NOTE, [reply(1, choice="r"), reply(2, text="rota boa")])
    assert praised is not None
    assert (praised.verdict, praised.reason) == (Verdict.RIGHT, "rota boa")


def test_text_replaces_the_button_reason():
    answer = feedback_from_replies(NOTE, [reply(1, choice="w0"), reply(2, text="é em Sorocaba")])
    assert answer is not None and answer.reason == "é em Sorocaba"


def test_unknown_or_inert_buttons_decide_nothing():
    assert feedback_from_replies(NOTE, [reply(1, choice="w7")]) is None
    assert feedback_from_replies(NOTE, [reply(1, choice="done")]) is None


def test_audit_notifications_produce_audit_feedback():
    audit = Notification(1, 100, "p1", FeedbackSource.AUDIT, NOW, ("x",))
    answer = feedback_from_replies(audit, [reply(1, choice="r")])
    assert answer is not None and answer.source is FeedbackSource.AUDIT


def test_answer_label():
    right = Feedback("p", Verdict.RIGHT, FeedbackSource.NOTIFICATION, NOW)
    assert answer_label(right) == "Registrado: ✓ Certo"
    assert answer_label(wrong("mudança")) == "Registrado: ✗ mudança"
