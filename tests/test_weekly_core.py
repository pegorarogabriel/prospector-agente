"""Regras puras da semana: silêncio, corte de orçamento, placar, auditoria e guardrails."""

import random
from datetime import UTC, date, datetime, timedelta

from prospector.core.budget import daily_budget
from prospector.core.feedback import (
    Feedback,
    FeedbackSource,
    Outcome,
    Verdict,
    silent_weeks,
    week_had_feedback,
)
from prospector.core.recipe import Recipe, RecipeStatus
from prospector.core.scoring import (
    ScoringCandidate,
    ScoringSettings,
    outcome_of,
    relevant_feedback,
    score_week,
)
from prospector.core.weekly import (
    AuditCandidate,
    choose_audit,
    rule_change_verdict,
    should_resume,
    week_id_for,
)

NOW = datetime(2026, 11, 29, 13, 0, tzinfo=UTC)


def _answer(days_ago: float, source=FeedbackSource.NOTIFICATION, verdict=Verdict.RIGHT):
    return Feedback("p", verdict, source, NOW - timedelta(days=days_ago))


# --- semana sem feedback ---------------------------------------------------


def test_week_had_feedback_looks_only_inside_the_window():
    start = NOW - timedelta(days=7)
    assert week_had_feedback([_answer(3)], start=start, end=NOW)
    assert not week_had_feedback([_answer(8)], start=start, end=NOW)
    assert week_had_feedback([_answer(1, FeedbackSource.AUDIT)], start=start, end=NOW)


def test_silence_is_zero_before_the_system_ever_asks():
    """O histórico importado é antigo; sem pergunta nova, não há silêncio."""
    old = NOW - timedelta(days=60)
    assert silent_weeks(last_answer=old, first_question=None, now=NOW) == 0


def test_silence_counts_from_the_first_question_when_answers_are_older():
    first = NOW - timedelta(days=15)
    old = NOW - timedelta(days=60)
    assert silent_weeks(last_answer=old, first_question=first, now=NOW) == 2


def test_silence_counts_from_the_last_answer():
    first = NOW - timedelta(days=60)
    last = NOW - timedelta(days=29)
    assert silent_weeks(last_answer=last, first_question=first, now=NOW) == 4


def test_budget_halves_after_four_silent_weeks():
    today = date(2026, 11, 1)  # 30 dias pela frente: 900 / 30 = 30
    assert daily_budget(0, today, silent_weeks=3) == 30
    assert daily_budget(0, today, silent_weeks=4) == 15


# --- placar ----------------------------------------------------------------


def _candidate(approved: bool, days_ago: float, recipes=("r1",)) -> ScoringCandidate:
    return ScoringCandidate("p", frozenset(recipes), approved, NOW - timedelta(days=days_ago))


def test_relevant_feedback_matches_the_question_that_was_asked():
    notification = _answer(1)
    audit = _answer(1, FeedbackSource.AUDIT, Verdict.WRONG)
    assert relevant_feedback(approved=True, feedback=[audit, notification]) is notification
    assert relevant_feedback(approved=False, feedback=[notification, audit]) is audit


def test_outcome_waits_for_the_reply_deadline():
    assert outcome_of(_candidate(True, 2), [], now=NOW) is Outcome.PENDING
    assert outcome_of(_candidate(True, 8), [], now=NOW) is Outcome.IGNORED
    assert outcome_of(_candidate(False, 1), [], now=NOW) is Outcome.FAILURE


def test_audit_saying_it_was_good_is_a_success_for_the_recipe():
    audit = _answer(1, FeedbackSource.AUDIT, Verdict.WRONG)
    assert outcome_of(_candidate(False, 2), [audit], now=NOW) is Outcome.SUCCESS


def test_score_week_decays_first_then_adds_full_weight():
    recipe = Recipe("r1", "agregado", successes=10, failures=10, status=RecipeStatus.ACTIVE)
    week = score_week(
        [recipe],
        [(frozenset({"r1"}), Outcome.SUCCESS), (frozenset({"r1"}), Outcome.IGNORED)],
        rng=random.Random(1),
    )
    [updated] = week.recipes
    assert updated.successes == 10 * 0.9 + 1
    assert updated.failures == 10 * 0.9
    assert week.successes["r1"] == 1


def test_post_found_by_two_recipes_counts_for_both():
    recipes = [
        Recipe("a", "a", status=RecipeStatus.ACTIVE),
        Recipe("b", "b", status=RecipeStatus.ACTIVE),
    ]
    week = score_week(recipes, [(frozenset({"a", "b"}), Outcome.FAILURE)], rng=random.Random(1))
    assert [r.failures for r in week.recipes] == [1, 1]


def test_retirement_opens_a_slot_and_only_one_candidate_is_promoted():
    champion = Recipe("lider", "x", successes=60, failures=20, status=RecipeStatus.ACTIVE)
    loser = Recipe("ruim", "y", successes=2, failures=90, status=RecipeStatus.ACTIVE)
    first = Recipe("cand1", "z", successes=3, failures=1)
    second = Recipe("cand2", "w")
    week = score_week(
        [champion, loser, first, second],
        [],
        rng=random.Random(1),
        settings=ScoringSettings(decay=1.0, max_active=4, samples=2000),
    )
    status = {r.id: r.status for r in week.recipes}
    assert week.retired == ["ruim"]
    assert week.promoted == ["cand1"]
    assert status["cand2"] is RecipeStatus.CANDIDATE


# --- auditoria e pausa -----------------------------------------------------


def test_audit_prefers_what_the_judge_saw():
    discards = [AuditCandidate("regra1", 0.0), AuditCandidate("juiz1", 0.3)]
    discards += [AuditCandidate("regra2", 0.0), AuditCandidate("juiz2", 0.1)]
    chosen = choose_audit(discards, rng=random.Random(3), size=2)
    assert sorted(chosen) == ["juiz1", "juiz2"]


def test_audit_fills_with_rule_discards_when_needed():
    chosen = choose_audit(
        [AuditCandidate("juiz", 0.4), AuditCandidate("regra", 0.0)], rng=random.Random(1)
    )
    assert chosen[0] == "juiz"
    assert set(chosen) == {"juiz", "regra"}


def test_resume_when_answered_or_after_deadline():
    later = NOW + timedelta(hours=22)
    assert should_resume(audit_answered=True, now=NOW, resume_after=later)
    assert not should_resume(audit_answered=False, now=NOW, resume_after=later)
    assert should_resume(audit_answered=False, now=later, resume_after=later)


def test_week_id_is_the_run_date():
    assert week_id_for(NOW) == "2026-11-29"


# --- guardrails ------------------------------------------------------------


def test_rule_change_cannot_lower_accuracy():
    why = rule_change_verdict(baseline_accuracy=0.8, proposed_accuracy=0.7, good_posts_lost=0)
    assert why is not None and "piorou" in why


def test_rule_change_cannot_lose_more_than_one_good_post():
    assert (
        rule_change_verdict(baseline_accuracy=0.8, proposed_accuracy=0.9, good_posts_lost=1) is None
    )
    why = rule_change_verdict(baseline_accuracy=0.8, proposed_accuracy=0.9, good_posts_lost=2)
    assert why is not None and "2 posts" in why


def test_rule_change_needs_something_to_measure():
    why = rule_change_verdict(baseline_accuracy=None, proposed_accuracy=None, good_posts_lost=0)
    assert why is not None
