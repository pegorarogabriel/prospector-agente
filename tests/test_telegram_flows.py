"""Envio dos aprovados e ingestão das respostas, com o Telegram falso."""

from datetime import UTC, datetime, timedelta

from prospector.core.feedback import Feedback, FeedbackSource, Verdict
from prospector.core.notification import DEFAULT_REJECT_REASONS, Notification, Reply
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe, RecipeStatus
from prospector.core.region import RegionStatus, RegionVerdict
from prospector.core.rules import RuleVerdict
from prospector.flows.collect import Decision
from prospector.flows.ingest_replies import confirm_replies, ingest_replies
from prospector.flows.notify import notify_approved
from prospector.integrations.fakes import FakeMessenger
from prospector.integrations.memory import InMemoryRepository

NOW = datetime(2026, 11, 25, 10, tzinfo=UTC)
KEEP = RuleVerdict(keep=True, region=RegionVerdict(RegionStatus.INSIDE, None, None, None))


def decision(post_id: str) -> Decision:
    post = Post(
        id=post_id,
        text="Agregamos truck baú",
        url=f"https://fb.com/{post_id}",
        posted_at=NOW - timedelta(hours=3),
        recipe_ids=frozenset({"r1"}),
    )
    return Decision(post, KEEP, TriageResult(True, 0.9, "procura truck", origin="Guarulhos"))


def repo_with_posts(*post_ids: str) -> InMemoryRepository:
    repository = InMemoryRepository()
    repository.add_recipes([Recipe("r1", "agregado truck", status=RecipeStatus.ACTIVE)])
    repository.add_posts([decision(post_id).post for post_id in post_ids])
    return repository


# --- envio -----------------------------------------------------------------


def test_sends_each_approved_and_remembers_the_message():
    repository = repo_with_posts("p1", "p2")
    messenger = FakeMessenger()
    report = notify_approved(repository, messenger, [decision("p1"), decision("p2")], now=NOW)

    assert [n.post_id for n in report.sent] == ["p1", "p2"]
    sent, message = messenger.sent[0]
    assert message.queries == ("agregado truck",)
    assert message.age == timedelta(hours=3)
    assert message.reject_reasons == DEFAULT_REJECT_REASONS
    stored = repository.find_notifications(sent.chat_id, [sent.message_id])
    assert stored[sent.message_id].post_id == "p1"
    assert stored[sent.message_id].reject_reasons == DEFAULT_REJECT_REASONS


def test_one_failed_send_does_not_stop_the_others():
    repository = repo_with_posts("p1", "p2")
    messenger = FakeMessenger()
    messenger.fail_for = {"p1"}
    report = notify_approved(repository, messenger, [decision("p1"), decision("p2")], now=NOW)
    assert [n.post_id for n in report.sent] == ["p2"]
    assert report.failed[0][0] == "p1"


# --- respostas -------------------------------------------------------------


def sent_note(repository: InMemoryRepository, post_id: str, message_id: int) -> None:
    repository.add_notifications(
        [
            Notification(
                1, message_id, post_id, FeedbackSource.NOTIFICATION, NOW, DEFAULT_REJECT_REASONS
            )
        ]
    )


def tap(update_id: int, message_id: int, choice: str) -> Reply:
    return Reply(update_id, 1, message_id, NOW, choice=choice)


def test_buttons_become_feedback_and_the_offset_moves():
    repository = repo_with_posts("p1", "p2")
    sent_note(repository, "p1", 100)
    sent_note(repository, "p2", 101)
    messenger = FakeMessenger()
    messenger.replies = [tap(7, 100, "r"), tap(8, 101, "w0"), tap(9, 555, "r")]

    report = ingest_replies(repository, messenger, write=True)

    assert report.recorded == 2
    assert report.unknown_message == 1
    assert {(f.post_id, f.verdict, f.reason) for f in repository.list_feedback()} == {
        ("p1", Verdict.RIGHT, None),
        ("p2", Verdict.WRONG, "Fora da região"),
    }
    assert repository.reply_offset() == 10

    confirm_replies(messenger, report)
    assert sorted(messenger.confirmed) == [
        (1, 100, "Registrado: ✓ Certo"),
        (1, 101, "Registrado: ✗ Fora da região"),
    ]


def test_running_again_reads_only_what_is_new():
    repository = repo_with_posts("p1")
    sent_note(repository, "p1", 100)
    messenger = FakeMessenger()
    messenger.replies = [tap(7, 100, "r")]
    ingest_replies(repository, messenger, write=True)

    again = ingest_replies(repository, messenger, write=True)
    assert again.replies_read == 0
    assert repository.reply_offset() == 8


def test_a_later_answer_does_not_overwrite_and_the_phone_shows_the_stored_one():
    repository = repo_with_posts("p1")
    sent_note(repository, "p1", 100)
    repository.add_feedback(
        [Feedback("p1", Verdict.RIGHT, FeedbackSource.NOTIFICATION, NOW, "rota boa")]
    )
    messenger = FakeMessenger()
    messenger.replies = [tap(7, 100, "w1")]

    report = ingest_replies(repository, messenger, write=True)

    assert report.feedback == []
    assert repository.list_feedback()[0].verdict is Verdict.RIGHT
    assert report.confirmations[0].label == "Registrado: ✓ rota boa"


def test_other_without_text_waits_and_is_counted():
    repository = repo_with_posts("p1")
    sent_note(repository, "p1", 100)
    messenger = FakeMessenger()
    messenger.replies = [tap(7, 100, "o")]
    report = ingest_replies(repository, messenger, write=True)
    assert report.waiting_reason == 1
    assert repository.list_feedback() == []


def test_dry_run_writes_nothing_and_keeps_the_offset():
    repository = repo_with_posts("p1")
    sent_note(repository, "p1", 100)
    messenger = FakeMessenger()
    messenger.replies = [tap(7, 100, "r")]
    report = ingest_replies(repository, messenger, write=False)
    assert len(report.feedback) == 1
    assert report.recorded == 0
    assert repository.list_feedback() == []
    assert repository.reply_offset() is None
