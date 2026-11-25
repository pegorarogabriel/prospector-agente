"""Coleta e triagem diária (arquitetura seções 3, 5 e 7), sem banco e sem Apify."""

import random
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from prospector.core.post import Post
from prospector.core.recipe import Recipe, RecipeStatus
from prospector.flows.collect import collect
from prospector.flows.ports import SearchResult, SearchRun
from prospector.integrations.fakes import FakeJudge, SampleSearchProvider
from prospector.integrations.memory import InMemoryRepository

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def recipe(recipe_id: str, query: str | None = None) -> Recipe:
    return Recipe(id=recipe_id, query=query or recipe_id, status=RecipeStatus.ACTIVE)


def good_post(post_id: str, text: str = "Agregamos truck baú em Guarulhos") -> Post:
    return Post(id=post_id, text=text, url=f"https://fb.com/{post_id}", posted_at=NOW)


class ScriptedSearch:
    """Devolve posts pré-definidos por termo e registra cada chamada."""

    def __init__(self, posts_by_query: dict[str, list[Post]], *, charge_extra: int = 0) -> None:
        self.posts_by_query = posts_by_query
        self.calls: list[tuple[str, int]] = []
        self.charge_extra = charge_extra

    def search(
        self, query: str, params: Mapping[str, str], *, limit: int, since: datetime
    ) -> SearchResult:
        self.calls.append((query, limit))
        posts = self.posts_by_query.get(query, [])[:limit]
        return SearchResult(posts=posts, charged=len(posts) + self.charge_extra)


class SpyJudge(FakeJudge):
    def __init__(self) -> None:
        self.judged: list[str] = []

    def judge(self, post, verdict):
        self.judged.append(post.id)
        return super().judge(post, verdict)


def repo_with(*recipes: Recipe) -> InMemoryRepository:
    repository = InMemoryRepository()
    repository.add_recipes(list(recipes))
    return repository


def run(repository, search, judge=None, **kwargs):
    options = {"record_spend": False, "write": False}
    options.update(kwargs)
    return collect(
        repository, search, judge or FakeJudge(), now=NOW, rng=random.Random(7), **options
    )


# --- orçamento ------------------------------------------------------------------


def test_budget_is_split_and_never_exceeded():
    search = ScriptedSearch({})
    report = run(repo_with(recipe("a"), recipe("b")), search)
    assert report.budget == 40  # teto diário: (900 / 7 dias) passaria disso
    assert sum(report.allocation.values()) == 40
    assert sum(limit for _, limit in search.calls) == 40


def test_spend_already_recorded_this_month_shrinks_the_budget():
    repository = repo_with(recipe("a"))
    repository.add_search_runs([SearchRun(NOW, "a", 880, 880, live=True)])
    report = run(repository, ScriptedSearch({}))
    assert report.spent_before == 880
    assert report.budget == 20 // 7


def test_lock_is_checked_before_every_search():
    """Se uma busca cobra mais que o pedido, a seguinte é barrada pela trava."""
    search = ScriptedSearch({}, charge_extra=1000)
    report = run(repo_with(recipe("a"), recipe("b")), search)
    assert len(report.allocation) == 2
    assert len(search.calls) == 1
    [skipped] = [s for s in report.searches if s.skipped_reason]
    assert skipped.skipped_reason == "sem orçamento"


def test_only_active_recipes_search():
    candidate = Recipe(id="c", query="c", status=RecipeStatus.CANDIDATE)
    search = ScriptedSearch({})
    run(repo_with(recipe("a"), candidate), search)
    assert {query for query, _ in search.calls} == {"a"}


# --- duplicados -------------------------------------------------------------------


def test_post_found_by_two_recipes_counts_for_both():
    shared = good_post("p1")
    search = ScriptedSearch({"a": [shared], "b": [shared]})
    report = run(repo_with(recipe("a"), recipe("b")), search)
    [decision] = report.decisions
    assert decision.post.recipe_ids == {"a", "b"}
    assert report.duplicates_merged == 1


def test_post_seen_in_a_previous_collection_is_not_judged_again():
    repository = repo_with(recipe("a"))
    repository.add_posts([good_post("old")])
    judge = SpyJudge()
    report = run(repository, ScriptedSearch({"a": [good_post("old"), good_post("new")]}), judge)
    assert report.already_seen == 1
    assert judge.judged == ["new"]


# --- regras e juiz ------------------------------------------------------------------


def test_rejected_by_rules_never_reaches_the_judge():
    judge = SpyJudge()
    sale = good_post("sale", "Vendo caminhão truck baú, único dono")
    report = run(repo_with(recipe("a")), ScriptedSearch({"a": [sale, good_post("ok")]}), judge)
    assert judge.judged == ["ok"]
    rejected = next(d for d in report.decisions if d.post.id == "sale")
    assert not rejected.result.approved
    assert not rejected.judged
    assert rejected.result.reason.startswith("não é agregamento")


def test_only_the_best_approved_go_to_telegram():
    posts = [good_post(f"p{i}") for i in range(6)]
    report = run(repo_with(recipe("a")), ScriptedSearch({"a": posts}))
    scores = [d.result.score for d in report.to_notify]
    assert len(report.to_notify) == 3
    assert scores == sorted(scores, reverse=True)
    best_left_out = max(d.result.score for d in report.decisions if d not in report.to_notify)
    assert best_left_out <= scores[-1]


# --- o que é gravado ------------------------------------------------------------------


def test_dry_run_writes_nothing():
    repository = repo_with(recipe("a"))
    run(repository, ScriptedSearch({"a": [good_post("p1")]}))
    assert repository.list_posts() == []
    assert repository.results_spent_since(NOW - timedelta(days=1)) == 0


def test_live_test_records_only_the_spend():
    """O crédito foi gasto: a trava precisa saber, mesmo sem gravar os posts."""
    repository = repo_with(recipe("a"))
    run(repository, ScriptedSearch({"a": [good_post("p1")]}), record_spend=True)
    assert repository.list_posts() == []
    assert repository.results_spent_since(NOW - timedelta(days=1)) == 1


def test_real_collection_writes_posts_and_decisions():
    repository = repo_with(recipe("a"))
    run(
        repository,
        ScriptedSearch({"a": [good_post("p1"), good_post("p2", "Vendo truck")]}),
        record_spend=True,
        write=True,
    )
    assert [post.id for post in repository.list_posts()] == ["p1", "p2"]
    decisions = {item.post_id: item.result.approved for item in repository.list_triage_results()}
    assert decisions == {"p1": True, "p2": False}


def test_live_test_uses_a_single_small_search():
    search = ScriptedSearch({})
    report = run(repo_with(recipe("a"), recipe("b")), search, record_spend=True, test_limit=5)
    assert len(search.calls) == 1
    assert search.calls[0][1] == 5
    assert report.budget == 5


# --- de ponta a ponta com os exemplos ---------------------------------------------------


def test_samples_exercise_every_rule_path():
    repository = repo_with(recipe("agregado truck"), recipe("agrega toco"))
    report = run(repository, SampleSearchProvider(now=NOW))
    reasons = {d.post.id: d.result.reason for d in report.decisions if not d.result.approved}
    approved = {d.post.id for d in report.decisions if d.result.approved}
    assert "fb-sample-002" in approved  # só imagem, com transcrição
    assert reasons.get("fb-sample-005") == "não está em português"
    assert reasons.get("fb-sample-008", "").startswith("post velho")
