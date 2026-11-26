"""Repositório em memória: a implementação fake da porta `Repository`.

Serve aos testes e ao `--dry-run` (simula a importação inteira sem tocar no
banco). Ele obedece às mesmas regras do Postgres — "insere se não existe",
uma resposta por post e por pergunta — e `tests/test_repository_contract.py`
roda os mesmos testes nos dois para garantir que o fake não mente.
"""

from collections.abc import Sequence
from datetime import datetime

from prospector.core.feedback import Feedback
from prospector.core.notification import Notification
from prospector.core.post import Post
from prospector.core.recipe import Recipe
from prospector.core.triage import TriageRule
from prospector.flows.ports import LabeledPost, SearchRun, TriagedPost


class InMemoryRepository:
    """Guarda tudo em dicionários, na ordem de inserção."""

    def __init__(self) -> None:
        self._recipes: dict[str, Recipe] = {}
        self._posts: dict[str, Post] = {}
        self._triage: dict[str, TriagedPost] = {}
        self._feedback: dict[tuple[str, str], Feedback] = {}
        self._search_runs: list[SearchRun] = []
        self._rules: dict[str, TriageRule] = {}
        self._notifications: dict[tuple[int, int], Notification] = {}
        self._reply_offset: int | None = None

    def add_recipes(self, recipes: Sequence[Recipe]) -> int:
        added = 0
        for recipe in recipes:
            if recipe.id not in self._recipes:
                self._recipes[recipe.id] = recipe
                added += 1
        return added

    def list_recipes(self) -> list[Recipe]:
        return sorted(self._recipes.values(), key=lambda recipe: recipe.id)

    def add_posts(self, posts: Sequence[Post]) -> int:
        added = 0
        for post in posts:
            if post.id not in self._posts:
                self._posts[post.id] = post
                added += 1
        return added

    def list_posts(self) -> list[Post]:
        return sorted(self._posts.values(), key=lambda post: post.id)

    def add_triage_results(self, triaged: Sequence[TriagedPost]) -> int:
        added = 0
        for item in triaged:
            if item.post_id not in self._triage:
                self._triage[item.post_id] = item
                added += 1
        return added

    def list_triage_results(self) -> list[TriagedPost]:
        return sorted(self._triage.values(), key=lambda item: item.post_id)

    def add_feedback(self, feedback: Sequence[Feedback]) -> int:
        added = 0
        for answer in feedback:
            key = (answer.post_id, answer.source.value)
            if key not in self._feedback:
                self._feedback[key] = answer
                added += 1
        return added

    def list_feedback(self) -> list[Feedback]:
        return sorted(
            self._feedback.values(), key=lambda answer: (answer.post_id, answer.source.value)
        )

    def existing_post_ids(self, ids: Sequence[str]) -> set[str]:
        return {post_id for post_id in ids if post_id in self._posts}

    def add_search_runs(self, runs: Sequence[SearchRun]) -> int:
        self._search_runs.extend(runs)
        return len(runs)

    def results_spent_since(self, moment: datetime) -> int:
        return sum(run.charged for run in self._search_runs if run.ran_at >= moment)

    def add_triage_rules(self, rules: Sequence[TriageRule]) -> int:
        added = 0
        for rule in rules:
            if rule.id not in self._rules:
                self._rules[rule.id] = rule
                added += 1
        return added

    def list_triage_rules(self) -> list[TriageRule]:
        return sorted(self._rules.values(), key=lambda rule: rule.id)

    def list_labeled_posts(self) -> list[LabeledPost]:
        labeled = []
        for answer in self.list_feedback():
            post = self._posts.get(answer.post_id)
            decision = self._triage.get(answer.post_id)
            if post is not None and decision is not None:
                labeled.append(LabeledPost(post, decision.result, answer))
        return labeled

    def add_notifications(self, notifications: Sequence[Notification]) -> int:
        added = 0
        for item in notifications:
            key = (item.chat_id, item.message_id)
            if key not in self._notifications:
                self._notifications[key] = item
                added += 1
        return added

    def find_notifications(
        self, chat_id: int, message_ids: Sequence[int]
    ) -> dict[int, Notification]:
        return {
            message_id: self._notifications[(chat_id, message_id)]
            for message_id in message_ids
            if (chat_id, message_id) in self._notifications
        }

    def reply_offset(self) -> int | None:
        return self._reply_offset

    def save_reply_offset(self, offset: int) -> None:
        self._reply_offset = offset
