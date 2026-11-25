"""Backtest do juiz contra as minhas respostas (arquitetura seção 5)."""

from datetime import UTC, datetime, timedelta

import pytest

from prospector.core.feedback import Feedback, FeedbackSource, Verdict, post_was_good
from prospector.core.post import Post, TriageResult
from prospector.flows.backtest import Confusion, choose_examples, run_backtest, to_example
from prospector.flows.ports import LabeledPost
from prospector.integrations.fakes import FakeJudge

START = datetime(2026, 9, 10, tzinfo=UTC)
GOOD_TEXT = "Precisamos de agregado truck baú para rotas em Guarulhos"
SALE_TEXT = "Promoção de velas aromáticas na cidade"


def labeled(n: int, *, good: bool, text: str | None = None, reason: str | None = None):
    """Post antigo que passou nas regras do antigo (aprovado) e a minha resposta."""
    post = Post(
        id=f"p{n}",
        text=GOOD_TEXT if text is None else text,
        url=f"https://fb.com/{n}",
        posted_at=START + timedelta(hours=n),
    )
    feedback = Feedback(
        post_id=post.id,
        verdict=Verdict.RIGHT if good else Verdict.WRONG,
        source=FeedbackSource.NOTIFICATION,
        answered_at=START + timedelta(hours=n, minutes=30),
        reason=reason if reason is not None else (None if good else "não serve"),
    )
    decision = TriageResult(approved=True, score=1.0, reason="passou")
    return LabeledPost(post, decision, feedback)


class RejectingJudge(FakeJudge):
    def judge(self, post, verdict):
        result = super().judge(post, verdict)
        return TriageResult(approved=False, score=0.1, reason="reprovei", vehicle=result.vehicle)


# --- gabarito e exemplos ------------------------------------------------------------


def test_post_was_good_follows_the_verdict():
    item = labeled(1, good=True)
    assert post_was_good(approved=item.decision.approved, feedback=item.feedback)


def test_good_answers_without_reason_still_become_examples():
    """Nos "chamei" eu quase nunca escrevia motivo."""
    assert to_example(labeled(1, good=True)).reason == "eu chamei"


def test_examples_are_left_out_of_the_measurement():
    history = [labeled(i, good=i % 3 == 0) for i in range(30)]
    report = run_backtest(history, lambda examples: FakeJudge(), examples_good=2, examples_bad=3)
    example_ids = {example.post_id for example in report.examples}
    measured_ids = {case.post.id for case in report.cases}
    assert len(example_ids) == 5
    assert not example_ids & measured_ids
    assert len(report.cases) == 25


def test_the_judge_is_built_with_the_chosen_examples():
    received = []
    history = [labeled(i, good=i % 2 == 0) for i in range(10)]

    def make_judge(examples):
        received.extend(examples)
        return FakeJudge()

    report = run_backtest(history, make_judge, examples_good=1, examples_bad=1)
    assert received == report.examples
    assert received == choose_examples(history, good=1, bad=1)


# --- os três recortes -----------------------------------------------------------------


def test_rules_judge_and_combined_are_measured_separately():
    history = [
        labeled(1, good=True),  # passa nas regras, bom
        labeled(2, good=False),  # passa nas regras, ruim
        labeled(3, good=False, text=SALE_TEXT),  # regras descartam, ruim
    ]
    report = run_backtest(history, lambda examples: FakeJudge(), examples_good=0, examples_bad=0)

    assert report.rules == Confusion(true_positive=1, false_positive=1, true_negative=1)
    assert report.judge == Confusion(true_positive=1, false_positive=1)
    assert report.combined == Confusion(true_positive=1, false_positive=1, true_negative=1)
    assert [case.post.id for case in report.judge_errors] == ["p2"]


def test_good_post_dropped_by_rules_is_reported():
    history = [labeled(1, good=True, text=SALE_TEXT)]
    report = run_backtest(history, lambda examples: FakeJudge(), examples_good=0, examples_bad=0)
    assert [case.post.id for case in report.rule_errors] == ["p1"]
    assert report.combined.false_negative == 1


def test_judge_rejections_count_as_false_negatives():
    history = [labeled(1, good=True)]
    report = run_backtest(
        history, lambda examples: RejectingJudge(), examples_good=0, examples_bad=0
    )
    assert report.judge == Confusion(false_negative=1)


def test_age_rule_uses_the_post_date():
    """O histórico é de setembro: medido "hoje", todo post cairia por ser velho."""
    report = run_backtest(
        [labeled(1, good=True)], lambda examples: FakeJudge(), examples_good=0, examples_bad=0
    )
    assert report.cases[0].rules.keep


def test_image_only_posts_are_skipped_and_counted():
    history = [labeled(1, good=True, text=""), labeled(2, good=True)]
    report = run_backtest(history, lambda examples: FakeJudge(), examples_good=0, examples_bad=0)
    assert report.skipped_image_only == 1
    assert [case.post.id for case in report.cases] == ["p2"]


def test_limit_keeps_good_and_bad_in_the_sample():
    history = [labeled(i, good=i < 3) for i in range(40)]
    report = run_backtest(
        history, lambda examples: FakeJudge(), limit=6, examples_good=0, examples_bad=0
    )
    goods = sum(case.good for case in report.cases)
    assert len(report.cases) == 6
    assert goods == 3
    assert report.skipped_by_limit == 34


# --- métricas ------------------------------------------------------------------------------


def test_confusion_metrics():
    confusion = Confusion(true_positive=8, false_positive=2, false_negative=4, true_negative=86)
    assert confusion.accuracy == pytest.approx(0.94)
    assert confusion.precision == pytest.approx(0.8)
    assert confusion.recall == pytest.approx(8 / 12)


def test_empty_metrics_are_none():
    assert Confusion().precision is None
    assert Confusion().accuracy is None
