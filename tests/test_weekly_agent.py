"""O grafo semanal de ponta a ponta, só com fakes.

Banco em memória, checkpoint em memória, Telegram falso, juiz falso e um LLM
roteirizado. Cada teste monta as dependências de novo entre o domingo e a
segunda, como acontece em produção (dois processos diferentes): é o checkpoint
que leva o grafo de um para o outro.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from prospector.agent.deps import WeeklyDeps
from prospector.agent.runner import resume_week, start_week
from prospector.core.feedback import Feedback, FeedbackSource, Verdict
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe, RecipeOrigin, RecipeStatus
from prospector.core.triage import TriageRule
from prospector.core.weekly import ChangeKind, RunStatus
from prospector.flows.ports import TriagedPost
from prospector.integrations.fakes import (
    FakeJudge,
    FakeMessenger,
    SampleSearchProvider,
    ScriptedChatModel,
)
from prospector.integrations.memory import InMemoryRepository

SUNDAY = datetime(2026, 11, 29, 13, 0, tzinfo=UTC)
MONDAY_EARLY = SUNDAY + timedelta(hours=10)
MONDAY_LATE = SUNDAY + timedelta(hours=23)

GOOD_TEXT = "Precisamos de agregado truck baú em Guarulhos para rota fixa {n}"
BAD_TEXT = "Contratamos agregado toco baú saindo de Guarulhos, pagamento {n}"


def _seed(repo: InMemoryRepository) -> None:
    """Receitas, regras, histórico rotulado (para o backtest) e a semana atual."""
    repo.add_recipes(
        [
            Recipe("r1", "agregado truck", status=RecipeStatus.ACTIVE, successes=5, failures=5),
            Recipe("r2", "agrega toco", status=RecipeStatus.ACTIVE, successes=1, failures=9),
        ]
    )
    repo.add_triage_rules([TriageRule("seed-01", "Aprove oferta de agregado truck ou toco baú.")])

    # Histórico rotulado de outubro: 10 bons e 10 ruins, todos aprovados.
    old = SUNDAY - timedelta(days=50)
    posts, decisions, answers = [], [], []
    for n in range(20):
        good = n % 2 == 0
        when = old + timedelta(hours=n)
        post_id = f"h{n}"
        text = (GOOD_TEXT if good else BAD_TEXT).format(n=n)
        posts.append(
            Post(post_id, text, f"https://fb/{post_id}", when, recipe_ids=frozenset({"r1"}))
        )
        decisions.append(TriagedPost(post_id, TriageResult(True, 0.7, "parece bom"), when))
        verdict = Verdict.RIGHT if good else Verdict.WRONG
        answers.append(
            Feedback(
                post_id, verdict, FeedbackSource.NOTIFICATION, when, "rota boa" if good else "golpe"
            )
        )
    repo.add_posts(posts)
    repo.add_triage_results(decisions)
    repo.add_feedback(answers)
    # O histórico já está no prior (a migração 007 faz isso no banco de verdade).
    repo.mark_scored([(post.id, "before-agent", "before_agent") for post in posts])

    # A semana: 3 descartes (2 do juiz, 1 das regras) e 1 aprovado notificado.
    week = SUNDAY - timedelta(days=3)
    repo.add_posts(
        [
            Post(
                f"w{n}",
                f"post da semana {n}",
                f"https://fb/w{n}",
                week,
                recipe_ids=frozenset({"r2"}),
            )
            for n in range(4)
        ]
    )
    repo.add_triage_results(
        [
            TriagedPost("w0", TriageResult(False, 0.3, "parece motorista"), week),
            TriagedPost("w1", TriageResult(False, 0.2, "sem carga"), week),
            TriagedPost("w2", TriageResult(False, 0.0, "post velho"), week),
            TriagedPost("w3", TriageResult(True, 0.9, "procura agregado"), week),
        ]
    )


def _deps(repo, messenger, model, *, now, make_judge=None) -> WeeklyDeps:
    return WeeklyDeps(
        repository=repo,
        messenger=messenger,
        model=model,
        make_judge=make_judge or (lambda rules, examples: FakeJudge()),
        search=SampleSearchProvider(now=now),
        now=lambda: now,
        rng=random.Random(7),
    )


def _call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


ANALYSIS = _call(
    "registrar_padroes",
    {
        "patterns": [
            {
                "description": "aprovou oferta de golpe",
                "count": 1,
                "post_ids": ["w3"],
                "direction": "aprovou_ruim",
            }
        ],
        "summary": "Um golpe passou pela triagem.",
    },
    "a1",
)


@pytest.fixture
def repo():
    repository = InMemoryRepository()
    _seed(repository)
    return repository


def test_sunday_sends_the_audit_and_pauses(repo):
    messenger, saver = FakeMessenger(), InMemorySaver()
    model = ScriptedChatModel()

    [report] = start_week(_deps(repo, messenger, model, now=SUNDAY), saver, now=SUNDAY)

    assert report.waiting
    audited = [message.post.id for _, message in messenger.sent]
    assert sorted(audited) == ["w0", "w1"]  # os que o juiz viu, não o das regras
    assert all(m.source is FeedbackSource.AUDIT for _, m in messenger.sent)
    assert repo.get_weekly_run("2026-11-29").status is RunStatus.WAITING
    assert model.received == []  # nenhum LLM no domingo


def test_resume_waits_until_answered_or_deadline(repo):
    saver = InMemorySaver()
    start_week(_deps(repo, FakeMessenger(), ScriptedChatModel(), now=SUNDAY), saver, now=SUNDAY)

    report = resume_week(
        _deps(repo, FakeMessenger(), ScriptedChatModel(), now=MONDAY_EARLY), saver, now=MONDAY_EARLY
    )
    assert report.waiting
    assert repo.get_weekly_run("2026-11-29").status is RunStatus.WAITING


def test_full_week_scores_proposes_validates_applies_and_summarizes(repo):
    saver = InMemorySaver()
    sunday_messenger = FakeMessenger()
    start_week(_deps(repo, sunday_messenger, ScriptedChatModel(), now=SUNDAY), saver, now=SUNDAY)

    # Segunda cedo: respondo a auditoria e o aprovado da semana.
    answered = SUNDAY + timedelta(hours=2)
    repo.add_feedback(
        [
            Feedback("w0", Verdict.WRONG, FeedbackSource.AUDIT, answered, "Era bom, eu chamaria"),
            Feedback("w1", Verdict.RIGHT, FeedbackSource.AUDIT, answered),
            Feedback("w3", Verdict.WRONG, FeedbackSource.NOTIFICATION, answered, "golpe"),
        ]
    )
    model = ScriptedChatModel(
        script=[
            ANALYSIS,
            _call("testar_regra", {"add": ["Reprove oferta com cara de golpe."]}, "t1"),
            _call(
                "registrar_propostas",
                {
                    "proposals": [
                        {
                            "kind": "add_rule",
                            "reason": "golpe aprovado",
                            "text": "Reprove oferta com cara de golpe.",
                        },
                        {
                            "kind": "add_recipe",
                            "reason": "testar truck",
                            "query": "agrego truck baú",
                        },
                        {"kind": "add_recipe", "reason": "segunda", "query": "outra busca"},
                    ],
                    "summary": "Uma regra contra golpe.",
                },
                "t2",
            ),
        ]
    )
    messenger = FakeMessenger()
    report = resume_week(_deps(repo, messenger, model, now=MONDAY_EARLY), saver, now=MONDAY_EARLY)

    assert not report.waiting
    run = repo.get_weekly_run("2026-11-29")
    assert run.status is RunStatus.DONE
    assert run.summary_message_id is not None

    # Placar: w0 era bom (acerto), w1 e w2 descartes (erro), w3 aprovado e ✗ (erro).
    r2 = next(r for r in repo.list_recipes() if r.id == "r2")
    assert r2.successes == pytest.approx(1 * 0.9 + 1)
    assert r2.failures == pytest.approx(9 * 0.9 + 3)

    changes = repo.list_changes("2026-11-29")
    assert [c.kind for c in changes] == [ChangeKind.ADD_RULE, ChangeKind.ADD_RECIPE]
    new_rule = next(r for r in repo.list_triage_rules() if r.created_by == "agent")
    assert new_rule.text == "Reprove oferta com cara de golpe."
    new_recipe = next(r for r in repo.list_recipes() if r.origin is RecipeOrigin.AGENT)
    assert new_recipe.status is RecipeStatus.CANDIDATE

    [(_, summary)] = messenger.texts
    assert "Reprove oferta com cara de golpe." in summary
    assert "limite de 1 busca nova" in summary  # a segunda busca foi barrada


def test_week_without_feedback_is_frozen_and_skips_the_llm(repo):
    saver = InMemorySaver()
    start_week(_deps(repo, FakeMessenger(), ScriptedChatModel(), now=SUNDAY), saver, now=SUNDAY)
    before = {r.id: (r.successes, r.failures) for r in repo.list_recipes()}

    model, messenger = ScriptedChatModel(script=[ANALYSIS]), FakeMessenger()
    resume_week(_deps(repo, messenger, model, now=MONDAY_LATE), saver, now=MONDAY_LATE)

    assert model.received == []
    assert {r.id: (r.successes, r.failures) for r in repo.list_recipes()} == before
    [(_, summary)] = messenger.texts
    assert "Sem feedback esta semana, nada mudou." in summary
    # Os descartes da semana congelada não voltam a ser cobrados depois.
    assert {c.post_id for c in repo.list_scoring_candidates()} == {"w3"}  # aprovado no prazo


def test_rule_that_worsens_the_backtest_is_rejected(repo):
    """Um juiz que obedece as regras: com "reprove tudo", ele perde os posts bons."""

    class RuleAwareJudge(FakeJudge):
        def __init__(self, rules):
            self._reject_all = any("tudo" in rule.text for rule in rules)

        def judge(self, post, verdict):
            result = super().judge(post, verdict)
            good = "Precisamos" in post.text
            approved = good and not self._reject_all
            return TriageResult(approved, result.score, result.reason)

    saver = InMemorySaver()
    start_week(_deps(repo, FakeMessenger(), ScriptedChatModel(), now=SUNDAY), saver, now=SUNDAY)
    repo.add_feedback([Feedback("w3", Verdict.WRONG, FeedbackSource.NOTIFICATION, SUNDAY, "golpe")])
    model = ScriptedChatModel(
        script=[
            ANALYSIS,
            _call(
                "registrar_propostas",
                {
                    "proposals": [
                        {"kind": "add_rule", "reason": "radical", "text": "Reprove tudo."},
                        {"kind": "deactivate_rule", "reason": "x", "rule_id": "nao-existe"},
                    ],
                    "summary": "",
                },
                "t1",
            ),
        ]
    )
    messenger = FakeMessenger()
    deps = _deps(
        repo,
        messenger,
        model,
        now=MONDAY_LATE,
        make_judge=lambda rules, examples: RuleAwareJudge(rules),
    )
    resume_week(deps, saver, now=MONDAY_LATE)

    assert repo.list_changes("2026-11-29") == []
    assert all(rule.created_by != "agent" for rule in repo.list_triage_rules())
    [(_, summary)] = messenger.texts
    assert "piorou o acerto" in summary
    assert "regra ativa não encontrada" in summary


def test_a_new_sunday_closes_the_previous_paused_week(repo):
    saver = InMemorySaver()
    start_week(_deps(repo, FakeMessenger(), ScriptedChatModel(), now=SUNDAY), saver, now=SUNDAY)
    next_sunday = SUNDAY + timedelta(days=7)

    reports = start_week(
        _deps(repo, FakeMessenger(), ScriptedChatModel(), now=next_sunday), saver, now=next_sunday
    )

    assert repo.get_weekly_run("2026-11-29").status is RunStatus.DONE
    assert repo.get_weekly_run("2026-12-06") is not None
    assert len(reports) == 2


def test_starting_the_same_week_twice_does_nothing(repo):
    saver, messenger = InMemorySaver(), FakeMessenger()
    start_week(_deps(repo, messenger, ScriptedChatModel(), now=SUNDAY), saver, now=SUNDAY)
    [report] = start_week(
        _deps(repo, messenger, ScriptedChatModel(), now=SUNDAY), saver, now=SUNDAY
    )
    assert "já iniciada" in report.status
    assert len(messenger.sent) == 2
