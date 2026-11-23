"""Lê o `prospector.db` do FacebookProspector (SQLite), só para leitura.

Ver `docs/arquitetura.md` seção 11.

O arquivo é aberto em modo somente leitura: a importação nunca pode alterar o
histórico de onde veio. Os campos de autor (`author_name`, `author_id`,
`author_profile_url`) nem são selecionados.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from prospector.flows.ports import LegacyPost

_POSTS_QUERY = """
SELECT id, post_text, url, post_datetime, collected_at, search_query,
       processing_status, filter_reason, vehicle, origin, destination,
       user_action, user_action_at, user_action_reason
FROM posts
ORDER BY id
"""

_MATCHES_QUERY = "SELECT post_id, search_query FROM post_search_matches"


def _parse(value: str | None) -> datetime | None:
    """O SQLAlchemy antigo gravava datas como texto ISO, sem fuso (UTC)."""
    return datetime.fromisoformat(value) if value else None


class LegacySqliteSource:
    """Implementa `LegacySource` sobre o arquivo SQLite antigo."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"banco antigo não encontrado: {path}")
        self._path = path

    def read_posts(self) -> list[LegacyPost]:
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            matches: dict[int, set[str]] = {}
            for post_id, query in conn.execute(_MATCHES_QUERY):
                matches.setdefault(post_id, set()).add(query)

            posts = []
            for row in conn.execute(_POSTS_QUERY):
                (
                    post_id,
                    text,
                    url,
                    posted_at,
                    collected_at,
                    query,
                    status,
                    filter_reason,
                    vehicle,
                    origin,
                    destination,
                    user_action,
                    user_action_at,
                    user_action_reason,
                ) = row
                queries = matches.get(post_id, set()) | ({query} if query else set())
                posts.append(
                    LegacyPost(
                        legacy_id=post_id,
                        text=text,
                        url=url,
                        posted_at=_parse(posted_at),
                        collected_at=_parse(collected_at) or datetime.min,
                        queries=frozenset(queries),
                        status=status,
                        filter_reason=filter_reason,
                        vehicle=vehicle,
                        origin=origin,
                        destination=destination,
                        user_action=user_action,
                        user_action_at=_parse(user_action_at),
                        user_action_reason=user_action_reason,
                    )
                )
        conn.close()
        return posts
