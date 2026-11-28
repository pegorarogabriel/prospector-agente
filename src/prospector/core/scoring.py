"""O placar da semana: o que cada post valeu para as receitas que o trouxeram.

Ver `docs/arquitetura.md` seções 3 e 6 (nó `atualizar_placares`). É código puro,
sem LLM: o agente semanal nunca mexe em placar.

A ordem da semana é sempre a mesma:

1. esquecimento: acertos e erros antigos valem `DECAY` do que valiam;
2. as observações novas entram com peso cheio;
3. aposentadoria das ativas que perderam de forma convincente para a líder;
4. promoção de no máximo uma candidata, se abriu vaga.

Esquecer antes de somar faz a evidência nova entrar inteira. O contrário
descontaria 10% de um dado que acabou de chegar.
"""

import random
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from prospector.core.bandit import (
    DECAY,
    PROBABILITY_SAMPLES,
    RETIREMENT_MIN_TRIALS,
    RETIREMENT_PROBABILITY_THRESHOLD,
    decay_all,
    leader,
    promote,
    retire,
    should_retire,
)
from prospector.core.feedback import (
    REPLY_DEADLINE,
    Feedback,
    FeedbackSource,
    Outcome,
    recipe_outcome,
)
from prospector.core.recipe import MAX_ACTIVE_RECIPES, Recipe, RecipeStatus

#: Candidatas promovidas por semana, no máximo (seção 3).
MAX_PROMOTIONS_PER_WEEK = 1


@dataclass(frozen=True)
class ScoringCandidate:
    """Um post decidido que ainda não entrou no placar."""

    post_id: str
    recipe_ids: frozenset[str]
    approved: bool
    decided_at: datetime


def relevant_feedback(*, approved: bool, feedback: Iterable[Feedback]) -> Feedback | None:
    """A resposta que vale para o placar: a da pergunta que eu de fato recebi.

    Post aprovado vira notificação; post descartado só chega a mim pela
    auditoria. Cada um tem uma pergunta só.
    """
    wanted = FeedbackSource.NOTIFICATION if approved else FeedbackSource.AUDIT
    return next((answer for answer in feedback if answer.source is wanted), None)


def outcome_of(
    candidate: ScoringCandidate, feedback: Iterable[Feedback], *, now: datetime
) -> Outcome:
    """O que o post valeu, ou `PENDING` se ainda está no prazo de resposta."""
    return recipe_outcome(
        approved=candidate.approved,
        feedback=relevant_feedback(approved=candidate.approved, feedback=feedback),
        age=now - candidate.decided_at,
        deadline=REPLY_DEADLINE,
    )


@dataclass(frozen=True)
class ScoringSettings:
    decay: float = DECAY
    max_active: int = MAX_ACTIVE_RECIPES
    min_trials: float = RETIREMENT_MIN_TRIALS
    threshold: float = RETIREMENT_PROBABILITY_THRESHOLD
    samples: int = PROBABILITY_SAMPLES
    max_promotions: int = MAX_PROMOTIONS_PER_WEEK


@dataclass(frozen=True)
class WeekScore:
    recipes: list[Recipe]
    successes: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    retired: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)


def score_week(
    recipes: Sequence[Recipe],
    outcomes: Sequence[tuple[frozenset[str], Outcome]],
    *,
    rng: random.Random,
    settings: ScoringSettings = ScoringSettings(),
) -> WeekScore:
    """Aplica uma semana de observações ao placar. Ver o docstring do módulo.

    `outcomes` traz, para cada post, as receitas que o trouxeram e o que ele
    valeu. `IGNORED` e `PENDING` não mexem em nada. Post achado por mais de uma
    receita conta para todas.
    """
    successes: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    for recipe_ids, outcome in outcomes:
        for recipe_id in recipe_ids:
            if outcome is Outcome.SUCCESS:
                successes[recipe_id] += 1
            elif outcome is Outcome.FAILURE:
                failures[recipe_id] += 1

    updated: list[Recipe] = []
    for recipe in decay_all(recipes, settings.decay):
        for _ in range(successes[recipe.id]):
            recipe = recipe.with_outcome(success=True)
        for _ in range(failures[recipe.id]):
            recipe = recipe.with_outcome(success=False)
        updated.append(recipe)

    champion = leader(updated)
    retired: list[str] = []
    if champion is not None:
        for index, recipe in enumerate(updated):
            if should_retire(
                recipe,
                champion,
                rng=rng,
                min_trials=settings.min_trials,
                threshold=settings.threshold,
                samples=settings.samples,
            ):
                updated[index] = retire(recipe)
                retired.append(recipe.id)

    promoted: list[str] = []
    active_count = sum(recipe.status is RecipeStatus.ACTIVE for recipe in updated)
    candidates = sorted(
        (r for r in updated if r.status is RecipeStatus.CANDIDATE),
        key=lambda r: (r.expected_rate, r.trials),
        reverse=True,
    )
    for candidate in candidates:
        if active_count >= settings.max_active or len(promoted) >= settings.max_promotions:
            break
        index = next(i for i, r in enumerate(updated) if r.id == candidate.id)
        updated[index] = promote(candidate)
        promoted.append(candidate.id)
        active_count += 1

    return WeekScore(
        recipes=updated,
        successes=successes,
        failures=failures,
        retired=retired,
        promoted=promoted,
    )
