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
from psycopg.types.json import Jsonb

from prospector.core.feedback import Feedback, FeedbackSource, Verdict
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe, RecipeOrigin, RecipeStatus
from prospector.flows.ports import SearchRun, TriagedPost

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
            INSERT INTO posts (id, text, image_url, url, posted_at, location)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                (post.id, post.text, post.image_url, post.url, post.posted_at, post.location)
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
            "SELECT id, text, image_url, url, posted_at, location FROM posts ORDER BY id"
        )
        return [
            Post(
                id=id_,
                text=text,
                image_url=image_url,
                url=url,
                posted_at=posted_at,
                location=location,
                recipe_ids=frozenset(recipe_ids.get(id_, set())),
            )
            for id_, text, image_url, url, posted_at, location in rows
        ]

    # --- triagem ----------------------------------------------------------

    def add_triage_results(self, triaged: Sequence[TriagedPost]) -> int:
        return self._insert_each(
            """
            INSERT INTO triage_results
                (post_id, approved, score, reason, vehicle, origin, destination)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
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
                )
                for item in triaged
            ],
        )

    def list_triage_results(self) -> list[TriagedPost]:
        rows = self._conn.execute(
            """
            SELECT post_id, approved, score, reason, vehicle, origin, destination
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
            )
            for post_id, approved, score, reason, vehicle, origin, destination in rows
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
