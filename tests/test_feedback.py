"""Testes da tradução do meu ✓/✗ em placar da receita.

Cada caso aqui é uma linha da tabela "O que conta como acerto e erro" da
arquitetura (seção 3). Se a tabela do documento mudar, estes testes falham — é
de propósito.
"""

from datetime import UTC, datetime, timedelta

import pytest

from prospector.core.feedback import (
    REPLY_DEADLINE,
    Feedback,
    FeedbackSource,
    Outcome,
    Verdict,
    is_triage_error,
    recipe_outcome,
)

ANSWERED_AT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def make_feedback(verdict: Verdict, source: FeedbackSource) -> Feedback:
    return Feedback(
        post_id="post-1",
        verdict=verdict,
        source=source,
        answered_at=ANSWERED_AT,
        reason="fora da região",
    )


def test_approved_and_confirmed_is_a_success():
    """Tabela: agente aprovou e eu marquei ✓ -> acerto."""
    outcome = recipe_outcome(
        approved=True,
        feedback=make_feedback(Verdict.RIGHT, FeedbackSource.NOTIFICATION),
        age=timedelta(hours=2),
    )
    assert outcome is Outcome.SUCCESS


def test_approved_and_rejected_is_a_failure():
    """Tabela: agente aprovou e eu marquei ✗ -> erro."""
    outcome = recipe_outcome(
        approved=True,
        feedback=make_feedback(Verdict.WRONG, FeedbackSource.NOTIFICATION),
        age=timedelta(hours=2),
    )
    assert outcome is Outcome.FAILURE


def test_discarded_without_audit_is_a_failure():
    """Tabela: agente descartou -> erro, a receita trouxe lixo.

    Vale mesmo que eu nunca veja o post. É o que dá volume suficiente pro placar
    convergir, e o que pune uma receita que só traz coisa ruim.
    """
    outcome = recipe_outcome(approved=False, feedback=None, age=timedelta(days=30))
    assert outcome is Outcome.FAILURE


def test_discarded_but_audit_says_it_was_good_is_a_success():
    """Tabela: descartado, caiu na auditoria e eu disse "era bom" -> acerto."""
    outcome = recipe_outcome(
        approved=False,
        feedback=make_feedback(Verdict.WRONG, FeedbackSource.AUDIT),
        age=timedelta(days=3),
    )
    assert outcome is Outcome.SUCCESS


def test_discarded_and_audit_confirms_the_discard_is_a_failure():
    """Auditoria confirmando o descarte não salva a receita: era lixo mesmo."""
    outcome = recipe_outcome(
        approved=False,
        feedback=make_feedback(Verdict.RIGHT, FeedbackSource.AUDIT),
        age=timedelta(days=3),
    )
    assert outcome is Outcome.FAILURE


def test_approved_without_answer_inside_the_deadline_is_pending():
    outcome = recipe_outcome(approved=True, feedback=None, age=timedelta(days=2))
    assert outcome is Outcome.PENDING


def test_approved_without_answer_past_the_deadline_is_ignored():
    """Tabela: aprovado e sem resposta minha em 7 dias -> ignorado.

    Ignorado, e não erro: meu silêncio não diz nada sobre a receita.
    """
    outcome = recipe_outcome(approved=True, feedback=None, age=REPLY_DEADLINE)
    assert outcome is Outcome.IGNORED


@pytest.mark.parametrize("days", [8, 30, 365])
def test_long_silence_stays_ignored(days):
    outcome = recipe_outcome(approved=True, feedback=None, age=timedelta(days=days))
    assert outcome is Outcome.IGNORED


def test_ignored_and_pending_never_touch_the_score():
    """Só acerto e erro entram no placar; os outros dois são 'ainda não sei'."""
    assert Outcome.IGNORED not in (Outcome.SUCCESS, Outcome.FAILURE)
    assert Outcome.PENDING not in (Outcome.SUCCESS, Outcome.FAILURE)


def test_triage_error_is_tracked_separately_from_the_recipe_score():
    """Um post bom descartado: acerto pra receita, erro pro juiz.

    Os dois placares são independentes de propósito — a receita achou algo bom
    e merece crédito; o juiz jogou fora e precisa de ajuste.
    """
    feedback = make_feedback(Verdict.WRONG, FeedbackSource.AUDIT)
    assert recipe_outcome(approved=False, feedback=feedback, age=timedelta(days=1)) is (
        Outcome.SUCCESS
    )
    assert is_triage_error(feedback=feedback)


def test_no_feedback_means_no_triage_error():
    """Sem resposta minha, não há como afirmar que o juiz errou."""
    assert not is_triage_error(feedback=None)


def test_confirming_the_agent_is_never_a_triage_error():
    assert not is_triage_error(feedback=make_feedback(Verdict.RIGHT, FeedbackSource.NOTIFICATION))
