"""Thompson Sampling, esquecimento e aposentadoria de receitas.

Ver `docs/arquitetura.md` seção 3.

Nada aqui importa biblioteca externa: `random` da biblioteca padrão basta, e por
isso o sorteio inteiro roda em milissegundos nos testes. Toda função que sorteia
recebe o gerador por parâmetro, para o teste poder fixar a semente.
"""

import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace

from prospector.core.recipe import Recipe, RecipeStatus

#: Fator de esquecimento semanal. O mercado muda; o que funcionou em março não
#: pode ficar travado como bom para sempre.
DECAY = 0.9

#: Mínimo de resultados por receita que ganhou alguma vaga, para não acionar o
#: actor do Apify por 1 post só.
MIN_RESULTS_PER_RECIPE = 5

#: Tentativas mínimas antes de uma receita poder ser aposentada.
RETIREMENT_MIN_TRIALS = 60

#: Aposenta se a probabilidade de ser melhor que a líder ficar abaixo disso.
RETIREMENT_PROBABILITY_THRESHOLD = 0.05

#: Amostras do Monte Carlo que estima P(receita > líder).
PROBABILITY_SAMPLES = 10_000


def active(recipes: Sequence[Recipe]) -> list[Recipe]:
    """Só as receitas que disputam o orçamento."""
    return [recipe for recipe in recipes if recipe.status is RecipeStatus.ACTIVE]


def leader(recipes: Sequence[Recipe]) -> Recipe | None:
    """A receita ativa com a melhor taxa esperada. É a régua da aposentadoria."""
    return max(active(recipes), key=lambda recipe: recipe.expected_rate, default=None)


def allocate(
    recipes: Sequence[Recipe],
    budget: int,
    *,
    rng: random.Random,
    min_per_recipe: int = MIN_RESULTS_PER_RECIPE,
) -> dict[str, int]:
    """Distribui `budget` resultados entre as receitas ativas (Thompson Sampling).

    Para cada vaga, sorteia um valor de `Beta(alpha, beta)` de cada receita; a
    maior leva a vaga. Receitas comprovadas vencem quase sempre; receitas
    incertas têm a Beta larga e às vezes sorteiam alto, o que as testa sem
    ninguém programar isso. Quando tudo começa a errar, as novas passam a
    vencer mais. Os "modos" conservador, exploratório e agressivo saem da
    incerteza, não de um `if`.

    A soma devolvida é exatamente `budget`: o orçamento é uma trava em código e
    não pode ser estourado nem por arredondamento. Receitas que ganharam poucas
    vagas são elevadas a `min_per_recipe`; se não couber, ficam de fora da
    rodada em vez de rodar o actor por 1 post.
    """
    candidates = active(recipes)
    if not candidates or budget <= 0:
        return {}

    wins: Counter[str] = Counter()
    for _ in range(budget):
        winner = max(candidates, key=lambda recipe: rng.betavariate(recipe.alpha, recipe.beta))
        wins[winner.id] += 1

    allocation: dict[str, int] = {}
    remaining = budget
    for recipe in sorted(candidates, key=lambda r: wins[r.id], reverse=True):
        if wins[recipe.id] == 0:
            continue
        if remaining < min_per_recipe:
            break
        share = min(max(wins[recipe.id], min_per_recipe), remaining)
        allocation[recipe.id] = share
        remaining -= share

    if not allocation:
        # Orçamento menor que o mínimo por receita: a mais sorteada leva tudo.
        best = max(candidates, key=lambda r: wins[r.id])
        return {best.id: budget}

    if remaining:
        top = max(allocation, key=lambda recipe_id: allocation[recipe_id])
        allocation[top] += remaining

    return allocation


def decay(recipe: Recipe, factor: float = DECAY) -> Recipe:
    """Esquece parte do passado da receita.

    Multiplica acertos e erros observados. O `+1` do prior não decai, porque ele
    não é observação: é a declaração de ignorância com que toda receita começa.
    Decair o prior junto empurraria as receitas para `Beta(0.9, 0.9)`, uma
    distribuição em U que afirma "é ótima ou é péssima" — o oposto de não saber.
    """
    return replace(
        recipe,
        successes=recipe.successes * factor,
        failures=recipe.failures * factor,
    )


def decay_all(recipes: Sequence[Recipe], factor: float = DECAY) -> list[Recipe]:
    """Aplica o esquecimento semanal em todas as receitas."""
    return [decay(recipe, factor) for recipe in recipes]


def probability_better(
    challenger: Recipe,
    champion: Recipe,
    *,
    rng: random.Random,
    samples: int = PROBABILITY_SAMPLES,
) -> float:
    """Estima P(taxa de `challenger` > taxa de `champion`) por Monte Carlo.

    Sorteia das duas Betas e conta quantas vezes a desafiante ganha. Dá para
    calcular isso de forma fechada com a função beta incompleta, mas isso traria
    o SciPy para dentro do `core` — e a estimativa com 10 mil amostras tem erro
    da ordem de 0.005, muito abaixo do limiar de 5% que decide a aposentadoria.
    """
    if challenger.id == champion.id:
        return 1.0
    wins = sum(
        rng.betavariate(challenger.alpha, challenger.beta)
        > rng.betavariate(champion.alpha, champion.beta)
        for _ in range(samples)
    )
    return wins / samples


def should_retire(
    recipe: Recipe,
    champion: Recipe,
    *,
    rng: random.Random,
    min_trials: float = RETIREMENT_MIN_TRIALS,
    threshold: float = RETIREMENT_PROBABILITY_THRESHOLD,
    samples: int = PROBABILITY_SAMPLES,
) -> bool:
    """A receita já perdeu de forma convincente o bastante para sair?

    Exige as duas coisas: evidência suficiente (`min_trials`) e uma chance baixa
    de ser melhor que a líder. Sem o mínimo de tentativas, uma receita nova que
    começasse com dois erros de azar seria aposentada antes de ter chance de
    provar valor — e aí o sistema pararia de explorar.

    A líder nunca se aposenta contra si mesma.
    """
    if recipe.status is not RecipeStatus.ACTIVE:
        return False
    if recipe.id == champion.id:
        return False
    if recipe.trials < min_trials:
        return False
    return probability_better(recipe, champion, rng=rng, samples=samples) < threshold


def retire(recipe: Recipe) -> Recipe:
    """Marca a receita como aposentada, abrindo uma vaga para uma candidata."""
    return replace(recipe, status=RecipeStatus.RETIRED)


def promote(recipe: Recipe) -> Recipe:
    """Ativa uma candidata. Quem controla o limite de ativas é quem chama."""
    return replace(recipe, status=RecipeStatus.ACTIVE)
