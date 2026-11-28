"""Implementação da porta `Repository` sobre Postgres (Supabase), e o migrador.

Ver `docs/arquitetura.md` seções 10 e 11.

Nenhum método faz commit: a transação é de quem abriu a conexão. Com
`psycopg.connect(...)` usado como `with`, tudo que acontece dentro do bloco
entra junto no fim — ou nada entra, se algo falhar no meio.
"""

from collections.abc import Sequence
from datetime import datetime
from importlib import resources

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from prospector.core.feedback import Feedback, FeedbackSource, Verdict
from prospector.core.notification import Notification
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe, RecipeOrigin, RecipeStatus
from prospector.core.scoring import ScoringCandidate
from prospector.core.triage import TriageRule
from prospector.core.weekly import Change, ChangeKind, RunStatus, WeeklyRun
from prospector.flows.ports import LabeledPost, SearchRun, TriagedPost

_REPLY_OFFSET_KEY = "telegram_reply_offset"

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


def migration_files() -> list[tuple[str, str]]:
    """(nome, SQL) de cada migração, em ordem. O prefixo numérico manda na ordem."""
    folder = resources.files("prospector.integrations") / "sql"
    files = sorted(
        (entry for entry in folder.iterdir() if entry.name.endswith(".sql")),
        key=lambda entry: entry.name,
    )
    return [(entry.name, entry.read_text(encoding="utf-8")) for entry in files]


def migrate(conn: psycopg.Connection) -> list[str]:
    """Aplica as migrações que faltam e devolve os nomes das que rodaram.

    Cada migração roda uma vez só: `schema_migrations` lembra quais já foram.
    Arquivo de migração aplicado não se edita — mudança nova é arquivo novo.
    """
    conn.execute(_MIGRATIONS_TABLE)
    applied = {name for (name,) in conn.execute("SELECT name FROM schema_migrations")}
    ran = []
    for name, sql in migration_files():
        if name in applied:
            continue
        conn.execute(sql)
        conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
        ran.append(name)
    return ran


class PostgresRepository:
    """Implementa `Repository`. Toda escrita é `ON CONFLICT DO NOTHING`."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def _insert_each(self, sql: str, rows: Sequence[tuple[object, ...]]) -> int:
        """Insere em lote e devolve quantas linhas entraram de fato.

        `executemany` usa o modo pipeline do psycopg 3: manda todas as linhas
        sem esperar a resposta de cada uma. Com o banco na nuvem, uma ida e
        volta por linha transformaria 1.000 posts em minutos de espera.
        """
        if not rows:
            return 0
        with self._conn.cursor() as cur:
            cur.executemany(sql, rows)
            return cur.rowcount

    # --- receitas ---------------------------------------------------------

    def add_recipes(self, recipes: Sequence[Recipe]) -> int:
        return self._insert_each(
            """
            INSERT INTO recipes (id, query, params, successes, failures, status, origin, parent_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                (
                    recipe.id,
                    recipe.query,
                    Jsonb(dict(recipe.params)),
                    recipe.successes,
                    recipe.failures,
                    recipe.status.value,
                    recipe.origin.value,
                    recipe.parent_id,
                )
                for recipe in recipes
            ],
        )

    def list_recipes(self) -> list[Recipe]:
        rows = self._conn.execute(
            """
            SELECT id, query, params, successes, failures, status, origin, parent_id
            FROM recipes ORDER BY id
            """
        )
        return [
            Recipe(
                id=id_,
                query=query,
                params=params,
                successes=successes,
                failures=failures,
                status=RecipeStatus(status),
                origin=RecipeOrigin(origin),
                parent_id=parent_id,
            )
            for id_, query, params, successes, failures, status, origin, parent_id in rows
        ]

    # --- posts ------------------------------------------------------------

    def add_posts(self, posts: Sequence[Post]) -> int:
        added = self._insert_each(
            """
            INSERT INTO posts (id, text, image_url, image_path, url, posted_at, location)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                (
                    post.id,
                    post.text,
                    post.image_url,
                    post.image_path,
                    post.url,
                    post.posted_at,
                    post.location,
                )
                for post in posts
            ],
        )
        self._insert_each(
            """
            INSERT INTO post_recipes (post_id, recipe_id) VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            [(post.id, recipe_id) for post in posts for recipe_id in sorted(post.recipe_ids)],
        )
        return added

    def list_posts(self) -> list[Post]:
        recipe_ids: dict[str, set[str]] = {}
        for post_id, recipe_id in self._conn.execute("SELECT post_id, recipe_id FROM post_recipes"):
            recipe_ids.setdefault(post_id, set()).add(recipe_id)
        rows = self._conn.execute(
            """
            SELECT id, text, image_url, image_path, url, posted_at, location
            FROM posts ORDER BY id
            """
        )
        return [
            Post(
                id=id_,
                text=text,
                image_url=image_url,
                image_path=image_path,
                url=url,
                posted_at=posted_at,
                location=location,
                recipe_ids=frozenset(recipe_ids.get(id_, set())),
            )
            for id_, text, image_url, image_path, url, posted_at, location in rows
        ]

    # --- triagem ----------------------------------------------------------

    def add_triage_results(self, triaged: Sequence[TriagedPost]) -> int:
        return self._insert_each(
            """
            INSERT INTO triage_results
                (post_id, approved, score, reason, vehicle, origin, destination, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()))
            ON CONFLICT (post_id) DO NOTHING
            """,
            [
                (
                    item.post_id,
                    item.result.approved,
                    item.result.score,
                    item.result.reason,
                    item.result.vehicle,
                    item.result.origin,
                    item.result.destination,
                    item.decided_at,
                )
                for item in triaged
            ],
        )

    def list_triage_results(self) -> list[TriagedPost]:
        rows = self._conn.execute(
            """
            SELECT post_id, approved, score, reason, vehicle, origin, destination, decided_at
            FROM triage_results ORDER BY post_id
            """
        )
        return [
            TriagedPost(
                post_id=post_id,
                result=TriageResult(
                    approved=approved,
                    score=score,
                    reason=reason,
                    vehicle=vehicle,
                    origin=origin,
                    destination=destination,
                ),
                decided_at=decided_at,
            )
            for post_id, approved, score, reason, vehicle, origin, destination, decided_at in rows
        ]

    # --- respostas --------------------------------------------------------

    def add_feedback(self, feedback: Sequence[Feedback]) -> int:
        return self._insert_each(
            """
            INSERT INTO feedback (post_id, verdict, source, answered_at, reason)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (post_id, source) DO NOTHING
            """,
            [
                (
                    answer.post_id,
                    answer.verdict.value,
                    answer.source.value,
                    answer.answered_at,
                    answer.reason,
                )
                for answer in feedback
            ],
        )

    def list_feedback(self) -> list[Feedback]:
        rows = self._conn.execute(
            """
            SELECT post_id, verdict, source, answered_at, reason
            FROM feedback ORDER BY post_id, source
            """
        )
        return [
            Feedback(
                post_id=post_id,
                verdict=Verdict(verdict),
                source=FeedbackSource(source),
                answered_at=answered_at,
                reason=reason,
            )
            for post_id, verdict, source, answered_at, reason in rows
        ]

    # --- buscas e orçamento -----------------------------------------------

    def existing_post_ids(self, ids: Sequence[str]) -> set[str]:
        if not ids:
            return set()
        rows = self._conn.execute("SELECT id FROM posts WHERE id = ANY(%s)", (list(ids),))
        return {post_id for (post_id,) in rows}

    def add_search_runs(self, runs: Sequence[SearchRun]) -> int:
        return self._insert_each(
            """
            INSERT INTO search_runs (ran_at, recipe_id, requested, charged, live)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [(run.ran_at, run.recipe_id, run.requested, run.charged, run.live) for run in runs],
        )

    def results_spent_since(self, moment: datetime) -> int:
        row = self._conn.execute(
            "SELECT coalesce(sum(charged), 0) FROM search_runs WHERE ran_at >= %s", (moment,)
        ).fetchone()
        return int(row[0]) if row else 0

    # --- regras e gabarito ------------------------------------------------

    def add_triage_rules(self, rules: Sequence[TriageRule]) -> int:
        return self._insert_each(
            """
            INSERT INTO triage_rules (id, text, active, created_by) VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            [(rule.id, rule.text, rule.active, rule.created_by) for rule in rules],
        )

    def list_triage_rules(self) -> list[TriageRule]:
        rows = self._conn.execute(
            "SELECT id, text, active, created_by FROM triage_rules ORDER BY id"
        )
        return [TriageRule(*row) for row in rows]

    def list_labeled_posts(self) -> list[LabeledPost]:
        posts = {post.id: post for post in self.list_posts()}
        decisions = {item.post_id: item.result for item in self.list_triage_results()}
        return [
            LabeledPost(posts[answer.post_id], decisions[answer.post_id], answer)
            for answer in self.list_feedback()
            if answer.post_id in posts and answer.post_id in decisions
        ]

    # --- telegram ---------------------------------------------------------

    def add_notifications(self, notifications: Sequence[Notification]) -> int:
        return self._insert_each(
            """
            INSERT INTO notifications
                (chat_id, message_id, post_id, source, sent_at, reject_reasons)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, message_id) DO NOTHING
            """,
            [
                (
                    item.chat_id,
                    item.message_id,
                    item.post_id,
                    item.source.value,
                    item.sent_at,
                    Jsonb(list(item.reject_reasons)),
                )
                for item in notifications
            ],
        )

    def find_notifications(
        self, chat_id: int, message_ids: Sequence[int]
    ) -> dict[int, Notification]:
        if not message_ids:
            return {}
        rows = self._conn.execute(
            """
            SELECT chat_id, message_id, post_id, source, sent_at, reject_reasons
            FROM notifications WHERE chat_id = %s AND message_id = ANY(%s)
            """,
            (chat_id, list(message_ids)),
        )
        return {
            message_id: Notification(
                chat_id=chat,
                message_id=message_id,
                post_id=post_id,
                source=FeedbackSource(source),
                sent_at=sent_at,
                reject_reasons=tuple(reasons),
            )
            for chat, message_id, post_id, source, sent_at, reasons in rows
        }

    def reply_offset(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM app_state WHERE key = %s", (_REPLY_OFFSET_KEY,)
        ).fetchone()
        return int(row[0]) if row else None

    def save_reply_offset(self, offset: int) -> None:
        self._conn.execute(
            """
            INSERT INTO app_state (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (_REPLY_OFFSET_KEY, str(offset)),
        )

    # --- agente semanal ---------------------------------------------------

    def update_recipes(self, recipes: Sequence[Recipe]) -> int:
        return self._insert_each(
            "UPDATE recipes SET successes = %s, failures = %s, status = %s WHERE id = %s",
            [(r.successes, r.failures, r.status.value, r.id) for r in recipes],
        )

    def set_rule_active(self, rule_id: str, active: bool) -> bool:
        cur = self._conn.execute(
            "UPDATE triage_rules SET active = %s WHERE id = %s", (active, rule_id)
        )
        return cur.rowcount > 0

    def first_notification_at(self) -> datetime | None:
        row = self._conn.execute("SELECT min(sent_at) FROM notifications").fetchone()
        return row[0] if row else None

    def notified_post_ids(self, source: FeedbackSource) -> set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT post_id FROM notifications WHERE source = %s", (source.value,)
        )
        return {post_id for (post_id,) in rows}

    def list_scoring_candidates(self) -> list[ScoringCandidate]:
        recipe_ids: dict[str, set[str]] = {}
        for post_id, recipe_id in self._conn.execute(
            """
            SELECT pr.post_id, pr.recipe_id FROM post_recipes pr
            WHERE NOT EXISTS (SELECT 1 FROM scored_posts s WHERE s.post_id = pr.post_id)
            """
        ):
            recipe_ids.setdefault(post_id, set()).add(recipe_id)
        rows = self._conn.execute(
            """
            SELECT t.post_id, t.approved, t.decided_at FROM triage_results t
            WHERE NOT EXISTS (SELECT 1 FROM scored_posts s WHERE s.post_id = t.post_id)
            ORDER BY t.post_id
            """
        )
        return [
            ScoringCandidate(post_id, frozenset(recipe_ids.get(post_id, set())), approved, when)
            for post_id, approved, when in rows
        ]

    def mark_scored(self, entries: Sequence[tuple[str, str, str]]) -> int:
        return self._insert_each(
            """
            INSERT INTO scored_posts (post_id, week_id, outcome) VALUES (%s, %s, %s)
            ON CONFLICT (post_id) DO NOTHING
            """,
            list(entries),
        )

    def add_weekly_run(self, run: WeeklyRun) -> int:
        return self._insert_each(
            f"""
            INSERT INTO weekly_runs ({_RUN_COLUMNS})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (week_id) DO NOTHING
            """,
            [_run_values(run)],
        )

    def get_weekly_run(self, week_id: str) -> WeeklyRun | None:
        row = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM weekly_runs WHERE week_id = %s", (week_id,)
        ).fetchone()
        return _run(row) if row else None

    def pending_weekly_run(self) -> WeeklyRun | None:
        row = self._conn.execute(
            f"""
            SELECT {_RUN_COLUMNS} FROM weekly_runs
            WHERE status = 'waiting' ORDER BY started_at LIMIT 1
            """
        ).fetchone()
        return _run(row) if row else None

    def update_weekly_run(self, run: WeeklyRun) -> None:
        week_id, *rest = _run_values(run)
        self._conn.execute(
            """
            UPDATE weekly_runs SET started_at = %s, resume_after = %s, status = %s,
                finished_at = %s, summary_chat_id = %s, summary_message_id = %s,
                undone_at = %s
            WHERE week_id = %s
            """,
            (*rest, week_id),
        )

    def find_weekly_run_by_message(self, chat_id: int, message_id: int) -> WeeklyRun | None:
        row = self._conn.execute(
            f"""
            SELECT {_RUN_COLUMNS} FROM weekly_runs
            WHERE summary_chat_id = %s AND summary_message_id = %s
            """,
            (chat_id, message_id),
        ).fetchone()
        return _run(row) if row else None

    def add_changes(self, changes: Sequence[Change]) -> int:
        return self._insert_each(
            """
            INSERT INTO changelog (week_id, kind, target_id, reason, detail, applied_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                (c.week_id, c.kind.value, c.target_id, c.reason, c.detail, c.applied_at)
                for c in changes
            ],
        )

    def list_changes(self, week_id: str) -> list[Change]:
        rows = self._conn.execute(
            """
            SELECT id, week_id, kind, target_id, reason, detail, applied_at, reverted_at
            FROM changelog WHERE week_id = %s ORDER BY id
            """,
            (week_id,),
        )
        return [
            Change(
                id=id_,
                week_id=week,
                kind=ChangeKind(kind),
                target_id=target,
                reason=reason,
                detail=detail,
                applied_at=applied,
                reverted_at=reverted,
            )
            for id_, week, kind, target, reason, detail, applied, reverted in rows
        ]

    def mark_changes_reverted(self, ids: Sequence[int], when: datetime) -> int:
        if not ids:
            return 0
        cur = self._conn.execute(
            "UPDATE changelog SET reverted_at = %s WHERE id = ANY(%s) AND reverted_at IS NULL",
            (when, list(ids)),
        )
        return cur.rowcount


_RUN_COLUMNS = (
    "week_id, started_at, resume_after, status, finished_at, "
    "summary_chat_id, summary_message_id, undone_at"
)


def _run_values(run: WeeklyRun) -> tuple[object, ...]:
    return (
        run.week_id,
        run.started_at,
        run.resume_after,
        run.status.value,
        run.finished_at,
        run.summary_chat_id,
        run.summary_message_id,
        run.undone_at,
    )


def _run(row: tuple) -> WeeklyRun:
    week_id, started, resume_after, status, finished, chat, message, undone = row
    return WeeklyRun(
        week_id=week_id,
        started_at=started,
        resume_after=resume_after,
        status=RunStatus(status),
        finished_at=finished,
        summary_chat_id=chat,
        summary_message_id=message,
        undone_at=undone,
    )


#: As tabelas que o `PostgresSaver.setup()` cria.
CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)


def secure_checkpoint_tables(conn: psycopg.Connection) -> None:
    """Liga o RLS nas tabelas que o `PostgresSaver` do LangGraph cria.

    O Supabase expõe pela API REST toda tabela do schema público. Os
    checkpoints guardam o estado do grafo semanal; mesmo sem texto de post,
    não há motivo para ficarem legíveis por lá (mesma regra da seção 10).
    """
    # A conexão do PostgresSaver devolve linhas como dicionário; aqui, tupla.
    with conn.cursor(row_factory=tuple_row) as cur:
        for table in CHECKPOINT_TABLES:
            exists = cur.execute("SELECT to_regclass(%s)", (table,)).fetchone()
            if exists and exists[0] is not None:
                cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
