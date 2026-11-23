"""Importa o histórico do FacebookProspector: receitas com prior, posts e respostas.

Ver `docs/arquitetura.md` seção 11.

A ideia central: o projeto antigo também tomava uma decisão por post (as regras
determinísticas aprovavam ou filtravam) e eu também respondia (CHAMEI ou
NAO_CHAMEI). Isso tem exatamente o formato do sistema novo. Então, em vez de
uma regra de contagem própria para o legado, cada post antigo é traduzido para
`TriageResult` + `Feedback` e passa pela mesma `recipe_outcome` do `core` que
vai pontuar os posts novos. Um único lugar decide o que é acerto.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from prospector.core.feedback import Feedback, FeedbackSource, Outcome, Verdict, recipe_outcome
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import MAX_ACTIVE_RECIPES, Recipe, RecipeOrigin, RecipeStatus
from prospector.flows.ports import LegacyPost, LegacySource, Repository, TriagedPost

#: Peso do histórico antigo no prior: dado velho vale menos. Com 0.2, cinco
#: acertos antigos valem o mesmo que um acerto novo.
LEGACY_PRIOR_WEIGHT = 0.2

#: Parâmetros do actor que o FacebookProspector usava em todas as buscas.
LEGACY_PARAMS = {"searchType": "latest"}

#: Meu veredito antigo sobre o POST (não sobre a decisão): o post era bom?
_POST_WAS_GOOD = {"CHAMEI": True, "NAO_CHAMEI": False}

_APPROVED_REASON = "Passou nas regras determinísticas do FacebookProspector"
_FILTERED_REASON = "Filtrado pelas regras determinísticas do FacebookProspector"


@dataclass(frozen=True)
class RecipeTally:
    """Quantos acertos e erros cada busca antiga acumulou, antes do peso."""

    successes: int = 0
    failures: int = 0


@dataclass(frozen=True)
class ImportReport:
    """O que a importação encontrou e o que de fato entrou no banco."""

    posts_read: int
    posts_without_text: int
    recipes_added: int
    posts_added: int
    triage_results_added: int
    feedback_added: int
    tallies: dict[str, RecipeTally] = field(default_factory=dict)
    """Placar cru por receita, para conferir o prior à mão."""


def recipe_id_for(query: str) -> str:
    """Id estável e legível: "Agregado Truck" vira `legacy-agregado-truck`.

    Estável porque a importação precisa reconhecer a mesma receita na segunda
    execução, e legível porque é o que aparece no resumo semanal.
    """
    ascii_query = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_query.lower()).strip("-")
    return f"legacy-{slug}"


def _as_utc(moment: datetime) -> datetime:
    """O banco antigo guardava UTC sem fuso. Aqui todo instante carrega o fuso."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _translate(old: LegacyPost) -> tuple[Post, TriagedPost, Feedback | None]:
    """Um post antigo no vocabulário do `core`.

    - `FILTERED` vira descarte; qualquer outro status foi para a fila, então
      vira aprovação.
    - CHAMEI/NAO_CHAMEI diz se o post era bom. O `Verdict` do sistema novo é
      sobre a DECISÃO, então ele é "certo" quando a decisão e o post concordam.
      Um post filtrado que eu chamei mesmo assim vira uma auditoria com
      "você errou" — exatamente o caso "descartado, mas era bom" da seção 3.
    """
    post_id = f"legacy-{old.legacy_id}"
    approved = old.status != "FILTERED"
    post = Post(
        id=post_id,
        text=old.text or "",
        url=old.url or "",
        posted_at=_as_utc(old.posted_at or old.collected_at),
        recipe_ids=frozenset(recipe_id_for(query) for query in old.queries),
    )
    triaged = TriagedPost(
        post_id=post_id,
        result=TriageResult(
            approved=approved,
            score=1.0 if approved else 0.0,
            reason=old.filter_reason or (_APPROVED_REASON if approved else _FILTERED_REASON),
            vehicle=old.vehicle,
            origin=old.origin,
            destination=old.destination,
        ),
    )

    was_good = _POST_WAS_GOOD.get(old.user_action or "")
    feedback = None
    if was_good is not None:
        feedback = Feedback(
            post_id=post_id,
            verdict=Verdict.RIGHT if approved == was_good else Verdict.WRONG,
            source=FeedbackSource.NOTIFICATION if approved else FeedbackSource.AUDIT,
            answered_at=_as_utc(old.user_action_at or old.collected_at),
            reason=old.user_action_reason,
        )
    return post, triaged, feedback


def _recipes_with_prior(
    queries: Sequence[str],
    tallies: dict[str, RecipeTally],
    *,
    weight: float,
    max_active: int,
) -> list[Recipe]:
    """Uma receita por busca antiga, com o placar encolhido pelo peso.

    Prior `Beta(1 + k·acertos, 1 + k·erros)`: o `+1` já vem de `Recipe.alpha`
    e `Recipe.beta`, então aqui só entra a parte `k·`.

    As `max_active` melhores pela taxa esperada começam ativas; as outras
    ficam candidatas, esperando uma aposentadoria abrir vaga.
    """
    recipes = []
    for query in queries:
        tally = tallies[recipe_id_for(query)]
        recipes.append(
            Recipe(
                id=recipe_id_for(query),
                query=query,
                params=dict(LEGACY_PARAMS),
                successes=weight * tally.successes,
                failures=weight * tally.failures,
                origin=RecipeOrigin.LEGACY,
            )
        )

    ranked = sorted(recipes, key=lambda recipe: (-recipe.expected_rate, -recipe.trials, recipe.id))
    active_ids = {recipe.id for recipe in ranked[:max_active]}
    return [
        replace(
            recipe,
            status=RecipeStatus.ACTIVE if recipe.id in active_ids else RecipeStatus.CANDIDATE,
        )
        for recipe in ranked
    ]


def import_legacy(
    source: LegacySource,
    repository: Repository,
    *,
    now: datetime,
    weight: float = LEGACY_PRIOR_WEIGHT,
    max_active: int = MAX_ACTIVE_RECIPES,
) -> ImportReport:
    """Lê o histórico antigo, calcula o prior das receitas e grava tudo.

    Reexecutável: o repositório só insere o que ainda não existe. Uma segunda
    execução não duplica posts nem reseta o placar de uma receita que já
    aprendeu com dados novos.
    """
    old_posts = source.read_posts()
    with_text = [old for old in old_posts if old.text and old.text.strip()]

    posts: list[Post] = []
    triaged: list[TriagedPost] = []
    feedback: list[Feedback] = []
    outcomes: dict[str, Counter[Outcome]] = {}
    query_by_id: dict[str, str] = {}

    for old in with_text:
        post, decision, answer = _translate(old)
        posts.append(post)
        triaged.append(decision)
        if answer is not None:
            feedback.append(answer)

        outcome = recipe_outcome(
            approved=decision.result.approved,
            feedback=answer,
            age=now - post.posted_at,
        )
        for query in old.queries:
            recipe_id = recipe_id_for(query)
            query_by_id.setdefault(recipe_id, query)
            outcomes.setdefault(recipe_id, Counter())[outcome] += 1

    tallies = {
        recipe_id: RecipeTally(
            successes=counts[Outcome.SUCCESS],
            failures=counts[Outcome.FAILURE],
        )
        for recipe_id, counts in outcomes.items()
    }
    recipes = _recipes_with_prior(
        sorted(query_by_id.values()), tallies, weight=weight, max_active=max_active
    )

    # Receitas primeiro: os posts apontam para elas.
    recipes_added = repository.add_recipes(recipes)
    posts_added = repository.add_posts(posts)
    return ImportReport(
        posts_read=len(old_posts),
        posts_without_text=len(old_posts) - len(with_text),
        recipes_added=recipes_added,
        posts_added=posts_added,
        triage_results_added=repository.add_triage_results(triaged),
        feedback_added=repository.add_feedback(feedback),
        tallies=tallies,
    )
