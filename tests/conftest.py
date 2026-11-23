"""Fixtures compartilhadas: um banco antigo de mentira e um Postgres isolado."""

import os
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

# Recorte do schema real do FacebookProspector: as colunas que a importação lê,
# mais as de autor, para provar que elas ficam para trás.
LEGACY_SCHEMA = """
CREATE TABLE posts (
    id INTEGER NOT NULL PRIMARY KEY,
    url TEXT,
    post_text TEXT,
    author_name VARCHAR(255),
    author_profile_url TEXT,
    post_datetime DATETIME,
    collected_at DATETIME NOT NULL,
    search_query VARCHAR(120),
    processing_status VARCHAR(20) NOT NULL,
    filter_reason VARCHAR(255),
    vehicle VARCHAR(60),
    origin VARCHAR(120),
    destination VARCHAR(120),
    user_action VARCHAR(20),
    user_action_at DATETIME,
    user_action_reason TEXT
);
CREATE TABLE post_search_matches (
    id INTEGER NOT NULL PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts (id),
    search_query VARCHAR(120) NOT NULL,
    first_seen_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL
);
"""


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    """Um `prospector.db` pequeno, no formato do antigo (datas em texto, sem fuso)."""
    path = tmp_path / "prospector.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.executemany(
            """
            INSERT INTO posts (id, url, post_text, author_name, author_profile_url,
                post_datetime, collected_at, search_query, processing_status,
                filter_reason, vehicle, origin, destination,
                user_action, user_action_at, user_action_reason)
            VALUES (?, ?, ?, 'Fulano', 'https://facebook.com/fulano', ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?)
            """,
            [
                (1, "https://fb.com/1", "Preciso agregado truck baú",
                 "2026-09-07 19:08:43.000000", "2026-09-08 01:56:46.616337",
                 "agregado truck", "QUEUED", None, "truck", "Guarulhos", "Campinas",
                 "CHAMEI", "2026-09-08 02:30:49.087998", "rota boa"),
                (2, "https://fb.com/2", "Vendo caminhão toco",
                 "2026-09-07 20:00:00.000000", "2026-09-08 01:56:47.000000",
                 "agrega toco", "FILTERED", "venda", None, None, None,
                 None, None, None),
                (3, None, None,
                 None, "2026-09-08 01:56:48.000000",
                 "agrega toco", "NO_DATE", None, None, None, None,
                 None, None, None),
            ],
        )  # fmt: skip
        conn.executemany(
            """
            INSERT INTO post_search_matches (post_id, search_query, first_seen_at, last_seen_at)
            VALUES (?, ?, '2026-09-08 01:56:46', '2026-09-08 01:56:46')
            """,
            [(1, "agregado truck"), (1, "agrega toco"), (2, "agrega toco")],
        )
    conn.close()
    return path


@pytest.fixture
def postgres_conn() -> Iterator[object]:
    """Uma conexão Postgres dentro de um schema temporário, apagado no fim.

    Só roda com `TEST_DATABASE_URL` definida (o CI sobe um Postgres para isso).
    O schema aleatório isola o teste: mesmo apontando para um banco que tem
    dados de verdade, nada fora dele é tocado.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL não definida")
    import psycopg

    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        try:
            with psycopg.connect(url, options=f"-c search_path={schema}") as conn:
                yield conn
        finally:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
