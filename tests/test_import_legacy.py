"""Importação do histórico antigo (arquitetura seção 11).

Cada post antigo vira decisão + resposta no vocabulário novo, e o placar sai
da mesma `recipe_outcome` que vai pontuar os posts novos.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from prospector.core.feedback import FeedbackSource, Verdict
from prospector.core.recipe import RecipeOrigin, RecipeStatus
from prospector.flows.import_legacy import (
    LEGACY_PRIOR_WEIGHT,
    RecipeTally,
    import_legacy,
    recipe_id_for,
)
from prospector.flows.ports import LegacyPost
from prospector.integrations.memory import InMemoryRepository

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
LONG_AGO = datetime(2026, 9, 10, 12, 0)  # naive, como o banco antigo gravava

BASE = LegacyPost(
    legacy_id=1,
    text="Preciso de agregado truck baú, Guarulhos x Campinas",
    url="https://facebook.com/posts/1",
    posted_at=LONG_AGO,
    collected_at=LONG_AGO,
    queries=frozenset({"agregado truck"}),
    status="QUEUED",
    filter_reason=None,
    vehicle="truck",
    origin="Guarulhos",
    destination="Campinas",
    user_action=None,
    user_action_at=None,
    user_action_reason=None,
)


def legacy(legacy_id: int, **changes) -> LegacyPost:
    return replace(BASE, legacy_id=legacy_id, **changes)


class FakeLegacySource:
    def __init__(self, posts: list[LegacyPost]) -> None:
        self._posts = posts

    def read_posts(self) -> list[LegacyPost]:
        return list(self._posts)


def run(posts: list[LegacyPost], **kwargs):
    repository = InMemoryRepository()
    report = import_legacy(FakeLegacySource(posts), repository, now=NOW, **kwargs)
    return report, repository


def tally_of(posts: list[LegacyPost], query: str = "agregado truck") -> RecipeTally:
    report, _ = run(posts)
    return report.tallies[recipe_id_for(query)]


# --- a tabela "o que conta como acerto e erro", aplicada ao legado ----------


def test_queued_and_called_is_success():
    assert tally_of([legacy(1, user_action="CHAMEI")]) == RecipeTally(successes=1)


def test_queued_and_not_called_is_failure():
    assert tally_of([legacy(1, user_action="NAO_CHAMEI")]) == RecipeTally(failures=1)


def test_filtered_is_failure():
    assert tally_of([legacy(1, status="FILTERED", filter_reason="venda")]) == RecipeTally(
        failures=1
    )


def test_filtered_but_called_is_success():
    """Descartado, mas era bom: acerto para a receita (seção 3)."""
    assert tally_of([legacy(1, status="FILTERED", user_action="CHAMEI")]) == RecipeTally(
        successes=1
    )


def test_queued_without_answer_is_ignored():
    """Aprovado e sem resposta há muito mais de 7 dias: não entra no placar."""
    assert tally_of([legacy(1)]) == RecipeTally()


def test_skip_is_not_a_decision():
    """PULAR era adiamento: conta como "sem resposta"."""
    assert tally_of([legacy(1, user_action="PULAR")]) == RecipeTally()


# --- respostas ----------------------------------------------------------------


def test_called_queued_post_becomes_right_notification():
    _, repo = run([legacy(1, user_action="CHAMEI", user_action_reason="rota boa")])
    [answer] = repo.list_feedback()
    assert answer.verdict is Verdict.RIGHT
    assert answer.source is FeedbackSource.NOTIFICATION
    assert answer.reason == "rota boa"


def test_called_filtered_post_becomes_wrong_audit():
    """O verdict é sobre a decisão: o filtro descartou um post bom, então errou."""
    _, repo = run([legacy(1, status="FILTERED", user_action="CHAMEI")])
    [answer] = repo.list_feedback()
    assert answer.verdict is Verdict.WRONG
    assert answer.source is FeedbackSource.AUDIT


def test_unanswered_posts_have_no_feedback():
    _, repo = run([legacy(1), legacy(2, status="FILTERED")])
    assert repo.list_feedback() == []


# --- prior das receitas ---------------------------------------------------------


def test_prior_is_weighted_by_k():
    """Beta(1 + k·acertos, 1 + k·erros), com k = 0.2."""
    posts = [legacy(i, user_action="CHAMEI") for i in range(10)]
    posts += [legacy(100 + i, status="FILTERED") for i in range(5)]
    _, repo = run(posts)
    [recipe] = repo.list_recipes()
    assert recipe.alpha == pytest.approx(1 + LEGACY_PRIOR_WEIGHT * 10)
    assert recipe.beta == pytest.approx(1 + LEGACY_PRIOR_WEIGHT * 5)
    assert recipe.origin is RecipeOrigin.LEGACY
    assert recipe.params == {"searchType": "latest"}


def test_post_found_by_two_queries_counts_for_both():
    _, repo = run([legacy(1, queries=frozenset({"agregado truck", "agrega toco"}))])
    assert {recipe.id for recipe in repo.list_recipes()} == {
        "legacy-agregado-truck",
        "legacy-agrega-toco",
    }
    [post] = repo.list_posts()
    assert post.recipe_ids == {"legacy-agregado-truck", "legacy-agrega-toco"}


def test_best_recipes_start_active_and_the_rest_candidate():
    queries = ["a", "b", "c", "d", "e"]
    posts = []
    for rank, query in enumerate(queries):
        # "a" acerta 5 de 5, "b" 4 de 5, ... "e" 1 de 5.
        for i in range(5):
            action = "CHAMEI" if i < 5 - rank else "NAO_CHAMEI"
            posts.append(legacy(rank * 10 + i, queries=frozenset({query}), user_action=action))
    _, repo = run(posts, max_active=4)
    status = {recipe.query: recipe.status for recipe in repo.list_recipes()}
    assert status == {
        "a": RecipeStatus.ACTIVE,
        "b": RecipeStatus.ACTIVE,
        "c": RecipeStatus.ACTIVE,
        "d": RecipeStatus.ACTIVE,
        "e": RecipeStatus.CANDIDATE,
    }


# --- tradução dos posts ---------------------------------------------------------


def test_timestamps_become_utc():
    _, repo = run([legacy(1, user_action="CHAMEI", user_action_at=LONG_AGO)])
    [post] = repo.list_posts()
    [answer] = repo.list_feedback()
    assert post.posted_at == LONG_AGO.replace(tzinfo=UTC)
    assert answer.answered_at.tzinfo is UTC


def test_post_without_date_uses_collection_time():
    collected = LONG_AGO + timedelta(hours=3)
    _, repo = run([legacy(1, status="NO_DATE", posted_at=None, collected_at=collected)])
    [post] = repo.list_posts()
    assert post.posted_at == collected.replace(tzinfo=UTC)


def test_posts_without_text_are_skipped():
    report, repo = run([legacy(1, text=None), legacy(2, text="   "), legacy(3)])
    assert report.posts_read == 3
    assert report.posts_without_text == 2
    assert [post.id for post in repo.list_posts()] == ["legacy-3"]


def test_filter_reason_is_kept_as_triage_reason():
    _, repo = run([legacy(1, status="FILTERED", filter_reason="venda de caminhão")])
    [decision] = repo.list_triage_results()
    assert decision.result.approved is False
    assert decision.result.reason == "venda de caminhão"


def test_recipe_id_is_ascii_slug():
    assert recipe_id_for("Agregamento Caminhão  3/4") == "legacy-agregamento-caminhao-3-4"


# --- reexecução -------------------------------------------------------------------


def test_running_twice_adds_nothing_the_second_time():
    repository = InMemoryRepository()
    source = FakeLegacySource([legacy(1, user_action="CHAMEI"), legacy(2, status="FILTERED")])
    first = import_legacy(source, repository, now=NOW)
    second = import_legacy(source, repository, now=NOW)
    assert (first.recipes_added, first.posts_added, first.feedback_added) == (1, 2, 1)
    assert (second.recipes_added, second.posts_added, second.feedback_added) == (0, 0, 0)


def test_rerun_does_not_reset_a_learned_score():
    """Se a receita já aprendeu com dado novo, reimportar não volta ao prior."""
    repository = InMemoryRepository()
    source = FakeLegacySource([legacy(1, user_action="CHAMEI")])
    import_legacy(source, repository, now=NOW)
    [learned] = repository.list_recipes()
    repository._recipes[learned.id] = replace(learned, successes=42.0)

    import_legacy(source, repository, now=NOW)
    [recipe] = repository.list_recipes()
    assert recipe.successes == 42.0
