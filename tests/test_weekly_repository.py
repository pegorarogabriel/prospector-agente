"""Contrato da porta `Repository` nos métodos do agente semanal.

Mesma ideia de `test_repository_contract.py`: os mesmos testes rodam no banco
em memória (sempre) e no Postgres (com `TEST_DATABASE_URL`).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from prospector.core.feedback import FeedbackSource
from prospector.core.notification import Notification
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe, RecipeStatus
from prospector.core.triage import TriageRule
from prospector.core.weekly import Change, ChangeKind, RunStatus, WeeklyRun
from prospector.flows.ports import Repository, SearchRun, TriagedPost
from prospector.integrations.memory import InMemoryRepository

WHEN = datetime(2026, 11, 20, 15, 30, tzinfo=UTC)
RECIPE = Recipe("r1", "agregado truck", successes=2.0, failures=1.0, status=RecipeStatus.ACTIVE)
POST = Post("p1", "Preciso agregado", "https://fb.com/1", WHEN, recipe_ids=frozenset({"r1"}))
DECISION = TriagedPost("p1", TriageResult(True, 0.8, "procura agregado"), WHEN)


@pytest.fixture(params=["memory", "postgres"])
def repository(request) -> Repository:
    if request.param == "memory":
        return InMemoryRepository()
    from prospector.integrations.postgres import PostgresRepository, migrate

    conn = request.getfixturevalue("postgres_conn")
    migrate(conn)
    return PostgresRepository(conn)


def test_update_recipes_changes_score_and_status_only_of_known(repository):
    repository.add_recipes([RECIPE])
    moved = replace(RECIPE, successes=9.5, failures=1.25, status=RecipeStatus.RETIRED)
    assert repository.update_recipes([moved, Recipe("nao-existe", "x")]) == 1
    assert repository.list_recipes() == [moved]


def test_set_rule_active(repository):
    repository.add_triage_rules([TriageRule("r-x", "regra x", True, "agent")])
    assert repository.set_rule_active("r-x", False)
    assert not repository.set_rule_active("nao-existe", False)
    rule = next(r for r in repository.list_triage_rules() if r.id == "r-x")
    assert rule.active is False


def test_scoring_candidates_leave_the_queue_once_marked(repository):
    repository.add_recipes([RECIPE])
    repository.add_posts([POST])
    repository.add_triage_results([DECISION])
    [candidate] = [c for c in repository.list_scoring_candidates() if c.post_id == "p1"]
    assert candidate.recipe_ids == POST.recipe_ids
    assert candidate.approved is True
    assert candidate.decided_at == WHEN
    assert repository.mark_scored([("p1", "2026-11-29", "success")]) == 1
    assert repository.mark_scored([("p1", "2026-12-06", "failure")]) == 0
    assert all(c.post_id != "p1" for c in repository.list_scoring_candidates())


def test_triage_without_time_gets_the_insert_time(repository):
    repository.add_recipes([RECIPE])
    repository.add_posts([POST])
    repository.add_triage_results([replace(DECISION, decided_at=None)])
    [stored] = repository.list_triage_results()
    assert stored.decided_at is not None


def test_first_notification_and_notified_posts(repository):
    assert repository.first_notification_at() is None
    repository.add_recipes([RECIPE])
    repository.add_posts([POST])
    audit = Notification(-1, 1, "p1", FeedbackSource.AUDIT, WHEN, ("Era bom",))
    later = Notification(-1, 2, "p1", FeedbackSource.NOTIFICATION, WHEN + timedelta(days=1), ())
    repository.add_notifications([later, audit])
    assert repository.first_notification_at() == WHEN
    assert repository.notified_post_ids(FeedbackSource.AUDIT) == {"p1"}


def test_weekly_runs_round_trip(repository):
    run = WeeklyRun("2026-11-29", WHEN, WHEN + timedelta(hours=22))
    assert repository.pending_weekly_run() is None
    assert repository.add_weekly_run(run) == 1
    assert repository.add_weekly_run(run) == 0
    assert repository.get_weekly_run("2026-11-29") == run
    assert repository.pending_weekly_run() == run
    done = replace(
        run,
        status=RunStatus.DONE,
        finished_at=WHEN + timedelta(days=1),
        summary_chat_id=-5,
        summary_message_id=42,
    )
    repository.update_weekly_run(done)
    assert repository.pending_weekly_run() is None
    assert repository.find_weekly_run_by_message(-5, 42) == done
    assert repository.find_weekly_run_by_message(-5, 43) is None


def test_changelog_round_trip_and_revert(repository):
    repository.add_weekly_run(WeeklyRun("2026-11-29", WHEN, WHEN))
    change = Change("2026-11-29", ChangeKind.ADD_RULE, "agent-1", "golpe", WHEN, "Reprove golpe.")
    assert repository.add_changes([change]) == 1
    [stored] = repository.list_changes("2026-11-29")
    assert stored.id is not None
    assert (stored.kind, stored.target_id, stored.detail) == (
        ChangeKind.ADD_RULE,
        "agent-1",
        "Reprove golpe.",
    )
    assert repository.mark_changes_reverted([stored.id], WHEN) == 1
    assert repository.mark_changes_reverted([stored.id], WHEN) == 0
    assert repository.list_changes("2026-11-29")[0].reverted_at == WHEN


def test_search_run_without_recipe_counts_in_the_month(repository):
    repository.add_search_runs([SearchRun(WHEN, None, 5, 5, live=True)])
    assert repository.results_spent_since(WHEN - timedelta(days=1)) == 5
