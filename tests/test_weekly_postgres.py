"""O agente semanal no Postgres de verdade: migração 007 e o `PostgresSaver`.

Só roda com `TEST_DATABASE_URL` (o CI sobe um Postgres descartável).
"""

import os
import random
from datetime import UTC, datetime, timedelta

from prospector.agent.deps import WeeklyDeps
from prospector.agent.runner import resume_week, start_week
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe, RecipeStatus
from prospector.core.weekly import RunStatus
from prospector.flows.ports import TriagedPost
from prospector.integrations.fakes import (
    FakeJudge,
    FakeMessenger,
    SampleSearchProvider,
    ScriptedChatModel,
)
from prospector.integrations.postgres import (
    PostgresRepository,
    migrate,
    migration_files,
    secure_checkpoint_tables,
)

SUNDAY = datetime(2026, 11, 29, 13, tzinfo=UTC)

LEGACY_POST = """
INSERT INTO posts (id, text, url, posted_at)
VALUES ('legacy-1', 'texto', 'https://fb/legacy-1', '2026-10-01T12:00:00Z')
"""


def test_migration_007_marks_existing_posts_as_already_scored(postgres_conn):
    """O histórico importado já está no prior: o placar não pode contá-lo de novo."""
    postgres_conn.execute(
        "CREATE TABLE schema_migrations (name text PRIMARY KEY, applied_at timestamptz)"
    )
    for name, sql in migration_files():
        if name < "007":
            postgres_conn.execute(sql)
            postgres_conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    postgres_conn.execute(LEGACY_POST)

    migrate(postgres_conn)

    rows = postgres_conn.execute("SELECT post_id, outcome FROM scored_posts").fetchall()
    assert rows == [("legacy-1", "before_agent")]


def test_weekly_graph_pauses_and_resumes_with_postgres_saver(postgres_conn):
    """O checkpoint de verdade: pausa numa conexão, retoma noutra."""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    migrate(postgres_conn)
    repo = PostgresRepository(postgres_conn)
    repo.add_recipes([Recipe("r1", "agregado", status=RecipeStatus.ACTIVE)])
    repo.add_posts([Post("w1", "post", "https://fb/w1", SUNDAY, recipe_ids=frozenset({"r1"}))])
    repo.add_triage_results(
        [TriagedPost("w1", TriageResult(False, 0.4, "x"), SUNDAY - timedelta(days=1))]
    )
    postgres_conn.commit()

    schema = postgres_conn.execute("SELECT current_schema()").fetchone()[0]

    def deps(now: datetime) -> WeeklyDeps:
        return WeeklyDeps(
            repository=repo,
            messenger=FakeMessenger(),
            model=ScriptedChatModel(),
            make_judge=lambda rules, examples: FakeJudge(),
            search=SampleSearchProvider(now=now),
            now=lambda: now,
            rng=random.Random(1),
        )

    def checkpoint_conn() -> psycopg.Connection:
        return psycopg.connect(
            os.environ["TEST_DATABASE_URL"],
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            options=f"-c search_path={schema}",
        )

    with checkpoint_conn() as conn:
        saver = PostgresSaver(conn)
        saver.setup()
        secure_checkpoint_tables(conn)
        [report] = start_week(deps(SUNDAY), saver, now=SUNDAY)
    assert report.waiting

    later = SUNDAY + timedelta(hours=23)
    with checkpoint_conn() as conn:
        report = resume_week(deps(later), PostgresSaver(conn), now=later)
    assert not report.waiting
    assert repo.get_weekly_run("2026-11-29").status is RunStatus.DONE

    secured = postgres_conn.execute(
        "SELECT relrowsecurity FROM pg_class WHERE relname = 'checkpoints' "
        "AND relnamespace = current_schema()::regnamespace"
    ).fetchone()
    assert secured == (True,)
