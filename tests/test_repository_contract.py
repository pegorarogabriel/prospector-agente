"""Teste de contrato da porta `Repository`.

Os mesmos testes rodam contra as duas implementações: a de memória (sempre) e
a de Postgres (quando há `TEST_DATABASE_URL`, como no CI). Se o fake passar a
se comportar diferente do banco real, os testes que usam o fake estariam
mentindo — e é aqui que isso aparece.
"""

from datetime import UTC, datetime

import pytest

from prospector.core.feedback import Feedback, FeedbackSource, Verdict
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe, RecipeOrigin, RecipeStatus
from prospector.flows.ports import Repository, TriagedPost
from prospector.integrations.memory import InMemoryRepository

WHEN = datetime(2026, 11, 20, 15, 30, tzinfo=UTC)

RECIPE = Recipe(
    id="legacy-agregado-truck",
    query="agregado truck",
    params={"searchType": "latest"},
    successes=2.4,
    failures=0.6,
    status=RecipeStatus.ACTIVE,
    origin=RecipeOrigin.LEGACY,
)
CHILD = Recipe(
    id="agent-agregado-truck-bau",
    query="agregado truck baú",
    origin=RecipeOrigin.AGENT,
    parent_id=RECIPE.id,
)
POST = Post(
    id="p1",
    text="Preciso agregado truck",
    url="https://fb.com/1",
    posted_at=WHEN,
    location="Guarulhos",
    recipe_ids=frozenset({RECIPE.id, CHILD.id}),
)
DECISION = TriagedPost(
    post_id="p1",
    result=TriageResult(approved=True, score=0.86, reason="procura agregado", vehicle="truck"),
)
ANSWER = Feedback(
    post_id="p1",
    verdict=Verdict.RIGHT,
    source=FeedbackSource.NOTIFICATION,
    answered_at=WHEN,
    reason="rota boa",
)


@pytest.fixture(params=["memory", "postgres"])
def repository(request) -> Repository:
    if request.param == "memory":
        return InMemoryRepository()
    from prospector.integrations.postgres import PostgresRepository, migrate

    conn = request.getfixturevalue("postgres_conn")
    migrate(conn)
    return PostgresRepository(conn)


def test_recipes_round_trip(repository):
    assert repository.add_recipes([RECIPE, CHILD]) == 2
    assert repository.list_recipes() == [CHILD, RECIPE]


def test_posts_round_trip_with_their_recipes(repository):
    repository.add_recipes([RECIPE, CHILD])
    assert repository.add_posts([POST]) == 1
    assert repository.list_posts() == [POST]


def test_triage_and_feedback_round_trip(repository):
    repository.add_recipes([RECIPE, CHILD])
    repository.add_posts([POST])
    assert repository.add_triage_results([DECISION]) == 1
    assert repository.add_feedback([ANSWER]) == 1
    assert repository.list_triage_results() == [DECISION]
    assert repository.list_feedback() == [ANSWER]


def test_adding_again_inserts_nothing_and_keeps_the_first(repository):
    repository.add_recipes([RECIPE])
    assert repository.add_recipes([Recipe(id=RECIPE.id, query="outra coisa")]) == 0
    assert repository.list_recipes() == [RECIPE]


def test_one_answer_per_post_and_question(repository):
    """Notificação e auditoria do mesmo post são respostas diferentes."""
    repository.add_recipes([RECIPE, CHILD])
    repository.add_posts([POST])
    audit = Feedback(
        post_id="p1", verdict=Verdict.WRONG, source=FeedbackSource.AUDIT, answered_at=WHEN
    )
    flipped = Feedback(
        post_id="p1",
        verdict=Verdict.WRONG,
        source=FeedbackSource.NOTIFICATION,
        answered_at=WHEN,
    )
    assert repository.add_feedback([ANSWER, audit, flipped]) == 2
    assert repository.list_feedback() == [audit, ANSWER]


def test_image_only_post_round_trips(repository):
    repository.add_recipes([RECIPE])
    flyer = Post(
        id="p2",
        text="",
        url="https://fb.com/2",
        posted_at=WHEN,
        image_url="https://fbcdn.net/flyer.jpg",
        recipe_ids=frozenset({RECIPE.id}),
    )
    assert repository.add_posts([flyer]) == 1
    assert repository.list_posts() == [flyer]
