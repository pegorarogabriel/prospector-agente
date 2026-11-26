"""Migrador do Postgres. Só roda com `TEST_DATABASE_URL` (o CI sobe um Postgres)."""

from prospector.integrations.postgres import migrate, migration_files


def test_migration_files_are_numbered_and_ordered():
    names = [name for name, _ in migration_files()]
    assert names == sorted(names)
    assert all(name[:3].isdigit() for name in names)


def test_migrate_applies_everything_once(postgres_conn):
    first = migrate(postgres_conn)
    second = migrate(postgres_conn)
    assert first == [name for name, _ in migration_files()]
    assert second == []


def test_every_table_has_row_level_security(postgres_conn):
    """No Supabase, tabela sem RLS fica legível pela API pública."""
    migrate(postgres_conn)
    rows = postgres_conn.execute(
        """
        SELECT relname, relrowsecurity FROM pg_class
        WHERE relnamespace = current_schema()::regnamespace
          AND relkind = 'r' AND relname <> 'schema_migrations'
        """
    ).fetchall()
    assert rows
    assert all(secured for _, secured in rows), rows


def test_initial_triage_rules_are_seeded(postgres_conn):
    from prospector.integrations.postgres import PostgresRepository

    migrate(postgres_conn)
    rules = PostgresRepository(postgres_conn).list_triage_rules()
    assert len(rules) == 7
    assert all(rule.created_by == "seed" and rule.active for rule in rules)
    assert any("motorista" in rule.text for rule in rules)
