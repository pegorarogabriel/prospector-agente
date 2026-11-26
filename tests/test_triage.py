"""Seleção dos exemplos few-shot do juiz."""

from datetime import UTC, datetime, timedelta

from prospector.core.triage import FewShotExample, select_examples

START = datetime(2026, 9, 1, tzinfo=UTC)


def example(n: int, *, good: bool, reason: str = "motivo", text: str = "texto") -> FewShotExample:
    return FewShotExample(
        post_id=f"p{n}",
        post_text=text,
        good=good,
        reason=reason,
        answered_at=START + timedelta(days=n),
    )


def test_balances_good_and_bad():
    labeled = [example(i, good=False) for i in range(10)] + [example(20, good=True)]
    chosen = select_examples(labeled, good=2, bad=3)
    assert sum(e.good for e in chosen) == 1
    assert sum(not e.good for e in chosen) == 3


def test_prefers_the_most_recent():
    labeled = [example(i, good=False) for i in range(5)]
    assert [e.post_id for e in select_examples(labeled, good=0, bad=2)] == ["p3", "p4"]


def test_needs_a_reason_and_a_text():
    labeled = [
        example(1, good=False, reason="  "),
        example(2, good=False, text=""),
        example(3, good=False),
    ]
    assert [e.post_id for e in select_examples(labeled, good=0, bad=5)] == ["p3"]


def test_excluded_posts_are_never_examples():
    """No backtest, o post medido não pode estar entre os exemplos."""
    labeled = [example(1, good=True), example(2, good=True)]
    chosen = select_examples(labeled, good=5, bad=0, exclude_ids=frozenset({"p2"}))
    assert [e.post_id for e in chosen] == ["p1"]


def test_result_is_in_chronological_order():
    labeled = [example(3, good=True), example(1, good=False), example(2, good=True)]
    chosen = select_examples(labeled, good=2, bad=1)
    assert [e.post_id for e in chosen] == ["p1", "p2", "p3"]
