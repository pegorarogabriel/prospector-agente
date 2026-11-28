"""Etapa 7: o resumo da semana com o botão "Desfazer ajustes"."""

import json
import random
from datetime import UTC, datetime, timedelta

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from prospector.agent.deps import WeeklyDeps
from prospector.agent.runner import resume_week, start_week
from prospector.core.feedback import Feedback, FeedbackSource, Verdict
from prospector.core.notification import UNDO_CHOICE, UNDONE_LABEL, Reply
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe, RecipeOrigin, RecipeStatus
from prospector.core.triage import TriageRule
from prospector.core.weekly import Change, ChangeKind, WeeklyRun
from prospector.flows.ingest_replies import ingest_replies
from prospector.flows.ports import TriagedPost
from prospector.flows.undo import undo_week
from prospector.integrations.fakes import (
    FakeJudge,
    FakeMessenger,
    SampleSearchProvider,
    ScriptedChatModel,
)
from prospector.integrations.memory import InMemoryRepository
from prospector.integrations.telegram import TelegramMessenger

NOW = datetime(2026, 11, 29, 13, tzinfo=UTC)
WEEK = "2026-11-29"


def _repo_with_changes() -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.add_recipes([Recipe("agent-nova", "agrego truck", origin=RecipeOrigin.AGENT)])
    repo.add_triage_rules(
        [
            TriageRule("seed-01", "regra antiga", active=False, created_by="seed"),
            TriageRule("agent-1", "regra nova", active=True, created_by="agent"),
        ]
    )
    repo.add_weekly_run(WeeklyRun(WEEK, NOW, NOW, summary_chat_id=1, summary_message_id=500))
    repo.add_changes(
        [
            Change(WEEK, ChangeKind.DEACTIVATE_RULE, "seed-01", "troca", NOW),
            Change(WEEK, ChangeKind.ADD_RULE, "agent-1", "troca", NOW, "regra nova"),
            Change(WEEK, ChangeKind.ADD_RECIPE, "agent-nova", "testar", NOW, "agrego truck"),
        ]
    )
    return repo


def test_undo_reverts_rules_and_recipes_of_the_week():
    repo = _repo_with_changes()
    report = undo_week(repo, WEEK, now=NOW)

    assert len(report.reverted) == 3
    active = {rule.id: rule.active for rule in repo.list_triage_rules()}
    assert active == {"seed-01": True, "agent-1": False}
    [recipe] = repo.list_recipes()
    assert recipe.status is RecipeStatus.RETIRED
    assert all(change.reverted_at == NOW for change in repo.list_changes(WEEK))
    assert repo.get_weekly_run(WEEK).undone_at == NOW


def test_undo_twice_does_nothing_the_second_time():
    repo = _repo_with_changes()
    undo_week(repo, WEEK, now=NOW)
    second = undo_week(repo, WEEK, now=NOW + timedelta(hours=1))
    assert second.already_undone
    assert second.reverted == []


def test_tapping_undo_on_the_summary_undoes_and_confirms():
    repo = _repo_with_changes()
    messenger = FakeMessenger()
    messenger.replies = [Reply(1, 1, 500, NOW, choice=UNDO_CHOICE)]

    report = ingest_replies(repo, messenger, write=True)

    assert report.undone_weeks == [WEEK]
    assert report.unknown_message == 0
    assert [c.label for c in report.confirmations] == [UNDONE_LABEL]
    assert {rule.id: rule.active for rule in repo.list_triage_rules()}["agent-1"] is False


def test_undo_in_dry_run_only_reports():
    repo = _repo_with_changes()
    messenger = FakeMessenger()
    messenger.replies = [Reply(1, 1, 500, NOW, choice=UNDO_CHOICE)]

    report = ingest_replies(repo, messenger, write=False)

    assert report.undone_weeks == [WEEK]
    assert repo.get_weekly_run(WEEK).undone_at is None


def test_undo_button_on_an_unknown_message_is_ignored():
    repo = _repo_with_changes()
    messenger = FakeMessenger()
    messenger.replies = [Reply(1, 1, 999, NOW, choice=UNDO_CHOICE)]
    report = ingest_replies(repo, messenger, write=True)
    assert report.undone_weeks == []
    assert report.unknown_message == 1


def _week_deps(repo, messenger, model, now):
    return WeeklyDeps(
        repository=repo,
        messenger=messenger,
        model=model,
        make_judge=lambda rules, examples: FakeJudge(),
        search=SampleSearchProvider(now=now),
        now=lambda: now,
        rng=random.Random(1),
    )


def test_summary_offers_undo_only_when_something_was_applied():
    """Semana congelada: nada aplicado, nada para desfazer, sem botão."""
    repo = InMemoryRepository()
    repo.add_recipes([Recipe("r1", "agregado", status=RecipeStatus.ACTIVE)])
    repo.add_posts([Post("w1", "x", "https://fb/w1", NOW, recipe_ids=frozenset({"r1"}))])
    repo.add_triage_results(
        [TriagedPost("w1", TriageResult(False, 0.3, "x"), NOW - timedelta(days=1))]
    )
    saver = InMemorySaver()
    start_week(_week_deps(repo, FakeMessenger(), ScriptedChatModel(), NOW), saver, now=NOW)
    messenger = FakeMessenger()
    later = NOW + timedelta(hours=23)
    resume_week(_week_deps(repo, messenger, ScriptedChatModel(), later), saver, now=later)

    assert len(messenger.texts) == 1
    assert messenger.undo_offered == []


def test_full_loop_apply_then_undo_from_the_phone():
    repo = InMemoryRepository()
    repo.add_recipes([Recipe("r1", "agregado", status=RecipeStatus.ACTIVE)])
    repo.add_posts([Post("w1", "x", "https://fb/w1", NOW, recipe_ids=frozenset({"r1"}))])
    repo.add_triage_results(
        [TriagedPost("w1", TriageResult(False, 0.3, "x"), NOW - timedelta(days=1))]
    )
    saver = InMemorySaver()
    start_week(_week_deps(repo, FakeMessenger(), ScriptedChatModel(), NOW), saver, now=NOW)
    repo.add_feedback(
        [Feedback("w1", Verdict.WRONG, FeedbackSource.AUDIT, NOW + timedelta(hours=1), "era bom")]
    )
    proposals = {
        "proposals": [{"kind": "add_recipe", "reason": "testar", "query": "agrego truck"}],
        "summary": "Uma busca nova.",
    }
    patterns = {
        "patterns": [
            {"description": "perdeu post bom", "count": 1, "post_ids": ["w1"], "direction": "x"}
        ],
        "summary": "Um post bom foi descartado.",
    }
    from langchain_core.messages import AIMessage

    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"name": "registrar_padroes", "args": patterns, "id": "1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "registrar_propostas", "args": proposals, "id": "2"}],
            ),
        ]
    )
    messenger = FakeMessenger(first_message_id=700)
    later = NOW + timedelta(hours=2)
    resume_week(_week_deps(repo, messenger, model, later), saver, now=later)

    assert messenger.undo_offered == [700]
    assert "desfaz" in messenger.texts[0][1]
    new = next(r for r in repo.list_recipes() if r.origin is RecipeOrigin.AGENT)
    assert new.status is RecipeStatus.CANDIDATE

    phone = FakeMessenger()
    phone.replies = [Reply(1, 1, 700, later, choice=UNDO_CHOICE)]
    ingest_replies(repo, phone, write=True)

    new = next(r for r in repo.list_recipes() if r.origin is RecipeOrigin.AGENT)
    assert new.status is RecipeStatus.RETIRED


def test_telegram_summary_has_the_undo_button_only_when_asked():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": 42}}}
        )

    messenger = TelegramMessenger(
        "t", 42, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    messenger.send_summary("resumo", undo=True)
    messenger.send_summary("resumo", undo=False)

    [[button]] = calls[0]["reply_markup"]["inline_keyboard"]
    assert button["callback_data"] == UNDO_CHOICE
    assert "reply_markup" not in calls[1]
