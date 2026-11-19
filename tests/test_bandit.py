"""Testes do sorteio, do esquecimento e da aposentadoria de receitas.

Todo teste que sorteia recebe um `random.Random` com semente fixa, então o
resultado é sempre o mesmo. As asserções são sobre propriedades (a soma fecha, a
comprovada ganha mais que a fraca) e não sobre números exatos — um teste que
dependesse de "a receita A recebeu exatamente 23" quebraria à primeira mudança
na ordem dos sorteios sem que nada de errado tivesse acontecido.
"""

import random

import pytest

from prospector.core.bandit import (
    DECAY,
    MIN_RESULTS_PER_RECIPE,
    RETIREMENT_MIN_TRIALS,
    allocate,
    decay,
    decay_all,
    leader,
    probability_better,
    promote,
    retire,
    should_retire,
)
from prospector.core.recipe import Recipe, RecipeStatus


def make_recipe(
    recipe_id: str,
    successes: float = 0.0,
    failures: float = 0.0,
    status: RecipeStatus = RecipeStatus.ACTIVE,
) -> Recipe:
    return Recipe(
        id=recipe_id,
        query=f"termo {recipe_id}",
        successes=successes,
        failures=failures,
        status=status,
    )


@pytest.fixture
def rng() -> random.Random:
    return random.Random(20260101)


# --------------------------------------------------------------------------
# Sorteio (Thompson Sampling)
# --------------------------------------------------------------------------


def test_allocation_sums_to_budget(rng):
    """O orçamento é trava: a soma distribuída tem que fechar exatamente."""
    recipes = [
        make_recipe("a", successes=40, failures=5),
        make_recipe("b", successes=10, failures=10),
        make_recipe("c", successes=2, failures=1),
        make_recipe("d"),
    ]
    for budget in (5, 7, 12, 30, 40, 123):
        allocation = allocate(recipes, budget, rng=rng)
        assert sum(allocation.values()) == budget, f"orçamento {budget}"


def test_allocation_never_goes_below_minimum(rng):
    """Ninguém aciona o actor do Apify por 1 post."""
    recipes = [make_recipe(name, successes=10, failures=10) for name in "abcd"]
    for budget in (10, 25, 40, 77):
        allocation = allocate(recipes, budget, rng=rng)
        assert all(share >= MIN_RESULTS_PER_RECIPE for share in allocation.values())


def test_allocation_ignores_inactive_recipes(rng):
    """Candidatas e aposentadas não disputam o orçamento."""
    recipes = [
        make_recipe("ativa", successes=5, failures=5),
        make_recipe("candidata", successes=50, failures=0, status=RecipeStatus.CANDIDATE),
        make_recipe("aposentada", successes=50, failures=0, status=RecipeStatus.RETIRED),
    ]
    allocation = allocate(recipes, 40, rng=rng)
    assert set(allocation) == {"ativa"}


def test_allocation_without_active_recipes_is_empty(rng):
    recipes = [make_recipe("x", status=RecipeStatus.CANDIDATE)]
    assert allocate(recipes, 40, rng=rng) == {}


def test_allocation_with_no_budget_is_empty(rng):
    recipes = [make_recipe("a", successes=5)]
    assert allocate(recipes, 0, rng=rng) == {}
    assert allocate(recipes, -10, rng=rng) == {}


def test_tiny_budget_goes_to_a_single_recipe(rng):
    """Orçamento menor que o mínimo: uma receita leva tudo em vez de ninguém rodar."""
    recipes = [make_recipe("a", successes=20, failures=2), make_recipe("b")]
    allocation = allocate(recipes, 3, rng=rng)
    assert len(allocation) == 1
    assert sum(allocation.values()) == 3


def test_proven_recipe_dominates_the_draw(rng):
    """Modo conservador: com evidência forte, a comprovada vence quase sempre.

    Ninguém programou esse comportamento — ele sai da Beta estreita de uma
    receita com muitas observações contra a Beta de uma que já se mostrou ruim.
    """
    proven = make_recipe("proven", successes=90, failures=10)
    weak = make_recipe("weak", successes=2, failures=30)
    allocation = allocate([proven, weak], 40, rng=rng)
    assert allocation["proven"] >= 30
    assert allocation.get("weak", 0) < allocation["proven"]


def test_unproven_recipe_gets_explored(rng):
    """Modo exploratório: uma receita nova ganha chances de provar valor.

    Contra uma líder morna, a receita sem histórico tem a Beta larga (uniforme)
    e sorteia alto com frequência. É o que impede o sistema de travar na
    primeira receita que funcionou.
    """
    lukewarm = make_recipe("lukewarm", successes=10, failures=10)
    fresh = make_recipe("fresh")
    allocation = allocate([lukewarm, fresh], 40, rng=rng)
    assert allocation.get("fresh", 0) >= MIN_RESULTS_PER_RECIPE


def test_allocation_is_reproducible_with_the_same_seed():
    recipes = [make_recipe("a", successes=8, failures=3), make_recipe("b", successes=1)]
    first = allocate(recipes, 40, rng=random.Random(7))
    second = allocate(recipes, 40, rng=random.Random(7))
    assert first == second


# --------------------------------------------------------------------------
# Esquecimento (decay)
# --------------------------------------------------------------------------


def test_decay_shrinks_observations(rng):
    decayed = decay(make_recipe("a", successes=100, failures=50))
    assert decayed.successes == pytest.approx(90.0)
    assert decayed.failures == pytest.approx(45.0)


def test_decay_does_not_touch_the_prior():
    """O `+1` não decai: ele é ignorância declarada, não observação.

    Se o prior decaísse junto, `alpha` cairia para 0.9 e a receita viraria uma
    `Beta(0.9, 0.9)` — uma distribuição em U que afirma "é ótima ou é péssima",
    exatamente o oposto de "não sei".
    """
    decayed = decay(make_recipe("a", successes=100, failures=50))
    assert decayed.alpha == pytest.approx(90.0 * 1 + 1.0)
    assert decayed.alpha != pytest.approx(DECAY * (100 + 1))
    assert decayed.beta == pytest.approx(46.0)


def test_decay_of_a_fresh_recipe_is_a_no_op():
    fresh = make_recipe("a")
    decayed = decay(fresh)
    assert decayed.successes == 0.0
    assert decayed.failures == 0.0
    assert decayed.alpha == 1.0
    assert decayed.beta == 1.0


def test_decay_moves_the_rate_toward_ignorance():
    """Esquecer aproxima a receita de 0.5 e derrete a confiança nela."""
    confident = make_recipe("a", successes=90, failures=10)
    decayed = decay(confident)
    assert decayed.trials < confident.trials
    assert abs(decayed.expected_rate - 0.5) < abs(confident.expected_rate - 0.5)


def test_repeated_decay_converges_to_the_prior():
    """Sem observações novas, um ano de esquecimento zera o histórico."""
    recipe = make_recipe("a", successes=200, failures=200)
    for _ in range(52):
        recipe = decay(recipe)
    assert recipe.trials < 2.0


def test_decay_preserves_identity_and_status():
    """Esquecer mexe no placar, não em quem a receita é."""
    original = make_recipe("a", successes=10, failures=10, status=RecipeStatus.ACTIVE)
    decayed = decay(original)
    assert decayed.id == original.id
    assert decayed.query == original.query
    assert decayed.status is original.status


def test_decay_all_applies_to_every_recipe():
    recipes = [make_recipe("a", successes=10), make_recipe("b", failures=20)]
    decayed = decay_all(recipes)
    assert [r.successes for r in decayed] == pytest.approx([9.0, 0.0])
    assert [r.failures for r in decayed] == pytest.approx([0.0, 18.0])


# --------------------------------------------------------------------------
# Aposentadoria
# --------------------------------------------------------------------------


def test_leader_is_the_best_active_recipe():
    best = make_recipe("best", successes=60, failures=6)
    worst = make_recipe("worst", successes=6, failures=60)
    hidden = make_recipe("hidden", successes=99, failures=0, status=RecipeStatus.CANDIDATE)
    assert leader([worst, best, hidden]) is best


def test_leader_without_active_recipes_is_none():
    assert leader([make_recipe("a", status=RecipeStatus.RETIRED)]) is None


def test_clear_loser_with_enough_evidence_retires(rng):
    champion = make_recipe("champion", successes=60, failures=6)
    loser = make_recipe("loser", successes=6, failures=60)
    assert loser.trials >= RETIREMENT_MIN_TRIALS
    assert should_retire(loser, champion, rng=rng)


def test_loser_without_enough_evidence_survives(rng):
    """Mesma taxa ruim, pouca evidência: pode ter sido azar. Fica."""
    champion = make_recipe("champion", successes=60, failures=6)
    unlucky = make_recipe("unlucky", successes=1, failures=5)
    assert unlucky.trials < RETIREMENT_MIN_TRIALS
    assert not should_retire(unlucky, champion, rng=rng)


def test_leader_never_retires_against_itself(rng):
    champion = make_recipe("champion", successes=60, failures=60)
    assert not should_retire(champion, champion, rng=rng)


def test_recipe_close_to_the_leader_survives(rng):
    """Perder por pouco não é perder de forma convincente."""
    champion = make_recipe("champion", successes=50, failures=50)
    challenger = make_recipe("challenger", successes=48, failures=52)
    assert challenger.trials >= RETIREMENT_MIN_TRIALS
    assert not should_retire(challenger, champion, rng=rng)


def test_already_retired_recipe_is_not_retired_again(rng):
    champion = make_recipe("champion", successes=60, failures=6)
    gone = make_recipe("gone", successes=6, failures=60, status=RecipeStatus.RETIRED)
    assert not should_retire(gone, champion, rng=rng)


def test_probability_better_is_near_half_for_identical_scores(rng):
    a = make_recipe("a", successes=20, failures=20)
    b = make_recipe("b", successes=20, failures=20)
    assert probability_better(a, b, rng=rng, samples=20_000) == pytest.approx(0.5, abs=0.02)


def test_probability_better_is_complementary(rng):
    a = make_recipe("a", successes=30, failures=10)
    b = make_recipe("b", successes=10, failures=30)
    forward = probability_better(a, b, rng=rng, samples=20_000)
    backward = probability_better(b, a, rng=rng, samples=20_000)
    assert forward + backward == pytest.approx(1.0, abs=0.02)
    assert forward > backward


def test_probability_better_against_itself_is_one(rng):
    a = make_recipe("a", successes=5, failures=5)
    assert probability_better(a, a, rng=rng) == 1.0


def test_retire_and_promote_only_change_status():
    recipe = make_recipe("a", successes=10, failures=2, status=RecipeStatus.CANDIDATE)
    activated = promote(recipe)
    assert activated.status is RecipeStatus.ACTIVE
    assert activated.successes == recipe.successes

    retired = retire(activated)
    assert retired.status is RecipeStatus.RETIRED
    assert retired.id == recipe.id
