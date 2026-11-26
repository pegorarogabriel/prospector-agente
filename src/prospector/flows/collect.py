"""Coleta e triagem diária.

Ver `docs/arquitetura.md` seções 3, 5 e 7. A sequência é sempre a mesma, por
isso é um script e não um agente:

1. quanto pode gastar hoje (o que sobra do mês / dias que faltam, com teto);
2. sorteio do orçamento entre as receitas ativas (Thompson Sampling);
3. uma busca por receita, com a trava de orçamento verificada ANTES de cada uma;
4. junção dos duplicados (o mesmo post achado por várias receitas conta para
   todas) e descarte do que já foi visto em coletas anteriores;
5. regras determinísticas em tudo; juiz só no que sobrou;
6. cópia da imagem dos posts que foram ao juiz (a URL do Facebook expira);
7. os aprovados com maior nota vão para o Telegram, até o teto diário.

Duas chaves controlam o que é gravado, para o mesmo código servir à coleta de
verdade e aos dois modos de dry-run:

- `record_spend`: grava o gasto em `search_runs`. Ligado sempre que a busca é
  real, porque o crédito do Apify foi consumido e a trava precisa saber.
- `write`: grava posts e decisões.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from prospector.core.bandit import allocate
from prospector.core.budget import (
    MAX_DAILY_RESULTS,
    MONTHLY_RESULT_BUDGET,
    can_spend,
    daily_budget,
)
from prospector.core.post import Post, TriageResult
from prospector.core.region import HOME_BASE, MAX_DISTANCE_KM
from prospector.core.rules import MAX_POST_AGE, RuleVerdict, apply_rules
from prospector.flows.ports import (
    ImageStore,
    Judge,
    Repository,
    SearchProvider,
    SearchRun,
    TriagedPost,
)

#: Quantos aprovados por dia vão para o Telegram (seção 5).
MAX_DAILY_NOTIFICATIONS = 3


@dataclass(frozen=True)
class CollectSettings:
    """Os limites da coleta. Os valores padrão são os da arquitetura."""

    monthly_budget: int = MONTHLY_RESULT_BUDGET
    daily_cap: int = MAX_DAILY_RESULTS
    max_notifications: int = MAX_DAILY_NOTIFICATIONS
    base: str = HOME_BASE
    max_km: float = MAX_DISTANCE_KM
    max_age: timedelta = MAX_POST_AGE


@dataclass(frozen=True)
class SearchSummary:
    """O que aconteceu numa busca."""

    recipe_id: str
    query: str
    requested: int
    charged: int = 0
    returned: int = 0
    skipped_reason: str | None = None
    """Preenchido quando a trava de orçamento impediu a busca."""


@dataclass(frozen=True)
class Decision:
    """O destino de um post novo: o laudo das regras e a decisão final."""

    post: Post
    rules: RuleVerdict
    result: TriageResult

    @property
    def judged(self) -> bool:
        """Passou nas regras e foi ao juiz."""
        return self.rules.keep


@dataclass(frozen=True)
class CollectReport:
    spent_before: int
    """Resultados já gastos no mês antes desta coleta."""

    budget: int
    """Quanto a coleta de hoje podia gastar."""

    allocation: dict[str, int]
    searches: list[SearchSummary]
    duplicates_merged: int
    """Posts achados por mais de uma receita nesta coleta."""

    already_seen: int
    """Posts que já estavam no banco, de coletas anteriores."""

    decisions: list[Decision]
    to_notify: list[Decision]
    recorded_spend: bool
    wrote: bool
    images_saved: int = 0
    images_failed: int = 0

    @property
    def charged(self) -> int:
        return sum(search.charged for search in self.searches)


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def _rejected(verdict: RuleVerdict) -> TriageResult:
    return TriageResult(
        approved=False,
        score=0.0,
        reason=verdict.reason or "descartado pelas regras",
        vehicle=verdict.vehicle,
        origin=verdict.region.origin,
        destination=verdict.region.destination,
    )


def collect(
    repository: Repository,
    search: SearchProvider,
    judge: Judge,
    *,
    now: datetime,
    rng: random.Random,
    settings: CollectSettings = CollectSettings(),
    record_spend: bool,
    write: bool,
    test_limit: int | None = None,
    image_store: ImageStore | None = None,
) -> CollectReport:
    """Roda uma coleta. Ver o docstring do módulo para a sequência.

    `test_limit` é o teste `--live` do dry-run: uma busca só, com poucos
    resultados, para a receita que o sorteio escolher. Continua passando pela
    trava de orçamento.

    `image_store`, com `write`, guarda a cópia da imagem dos posts que foram ao
    juiz. Só desses: os barrados pelas regras são lixo claro, e o plano grátis
    do Storage tem 1 GB.
    """
    spent = repository.results_spent_since(_month_start(now))
    spent_before = spent
    budget = daily_budget(
        spent, now.date(), monthly_budget=settings.monthly_budget, cap=settings.daily_cap
    )
    recipes = {recipe.id: recipe for recipe in repository.list_recipes()}

    if test_limit is not None:
        budget = min(test_limit, budget)
        allocation = allocate(
            list(recipes.values()), budget, rng=rng, min_per_recipe=max(budget, 1)
        )
    else:
        allocation = allocate(list(recipes.values()), budget, rng=rng)

    found: dict[str, Post] = {}
    duplicates = 0
    searches: list[SearchSummary] = []
    runs: list[SearchRun] = []
    since = now - settings.max_age

    for recipe_id, amount in allocation.items():
        recipe = recipes[recipe_id]
        if not can_spend(amount, spent, monthly_budget=settings.monthly_budget):
            searches.append(
                SearchSummary(recipe_id, recipe.query, amount, skipped_reason="sem orçamento")
            )
            continue

        result = search.search(recipe.query, recipe.params, limit=amount, since=since)
        spent += result.charged
        runs.append(SearchRun(now, recipe_id, amount, result.charged, live=record_spend))
        searches.append(
            SearchSummary(recipe_id, recipe.query, amount, result.charged, len(result.posts))
        )

        for post in result.posts:
            if post.id in found:
                duplicates += 1
                merged = found[post.id].recipe_ids | {recipe_id}
                found[post.id] = replace(found[post.id], recipe_ids=merged)
            else:
                found[post.id] = replace(post, recipe_ids=post.recipe_ids | {recipe_id})

    seen = repository.existing_post_ids(list(found))
    new_posts = [post for post_id, post in found.items() if post_id not in seen]

    decisions: list[Decision] = []
    for post in new_posts:
        verdict = apply_rules(
            post, now=now, base=settings.base, max_km=settings.max_km, max_age=settings.max_age
        )
        result = judge.judge(post, verdict) if verdict.keep else _rejected(verdict)
        decisions.append(Decision(post, verdict, result))

    saved = failed = 0
    if write and image_store is not None:
        decisions, saved, failed = _store_images(image_store, decisions)

    approved = sorted(
        (decision for decision in decisions if decision.result.approved),
        key=lambda decision: decision.result.score,
        reverse=True,
    )

    if record_spend:
        repository.add_search_runs(runs)
    if write:
        _save(repository, decisions)

    return CollectReport(
        spent_before=spent_before,
        budget=budget,
        allocation=allocation,
        searches=searches,
        duplicates_merged=duplicates,
        already_seen=len(seen),
        decisions=decisions,
        to_notify=approved[: settings.max_notifications],
        recorded_spend=record_spend,
        wrote=write,
        images_saved=saved,
        images_failed=failed,
    )


def _store_images(store: ImageStore, decisions: list[Decision]) -> tuple[list[Decision], int, int]:
    """Copia as imagens dos posts julgados e devolve as decisões com o caminho."""
    result: list[Decision] = []
    saved = failed = 0
    for decision in decisions:
        if not (decision.judged and decision.post.image_url):
            result.append(decision)
            continue
        path = store.save(decision.post)
        if path is None:
            failed += 1
            result.append(decision)
            continue
        saved += 1
        result.append(replace(decision, post=replace(decision.post, image_path=path)))
    return result, saved, failed


def _save(repository: Repository, decisions: Sequence[Decision]) -> None:
    repository.add_posts([decision.post for decision in decisions])
    repository.add_triage_results(
        [TriagedPost(decision.post.id, decision.result) for decision in decisions]
    )
