"""Portas: o que os casos de uso precisam do mundo, sem saber quem entrega.

Cada porta é um `Protocol` — uma interface estrutural. Quem a implementa não
precisa herdar dela: basta ter os mesmos métodos. As implementações concretas
moram em `integrations/` (Postgres, SQLite antigo, memória), e os flows só
conhecem estas assinaturas. É isso que deixa os testes rodarem sem banco.

Ver `docs/arquitetura.md` seção 9.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from prospector.core.feedback import Feedback
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe


@dataclass(frozen=True)
class TriagedPost:
    """A decisão da triagem amarrada ao post sobre o qual ela foi tomada."""

    post_id: str
    result: TriageResult


class Repository(Protocol):
    """Onde o estado do sistema vive: receitas, posts, decisões e respostas.

    Toda escrita é "insere se ainda não existe" e devolve quantos registros
    entraram de fato. Isso torna as rotinas reexecutáveis: rodar a importação
    duas vezes não duplica nada, nem sobrescreve um placar que já aprendeu.

    Nenhum método faz commit. Quem abre a conexão decide onde começa e termina
    a transação — a importação inteira, por exemplo, entra ou não entra.
    """

    def add_recipes(self, recipes: Sequence[Recipe]) -> int: ...

    def list_recipes(self) -> list[Recipe]: ...

    def add_posts(self, posts: Sequence[Post]) -> int: ...

    def list_posts(self) -> list[Post]: ...

    def add_triage_results(self, triaged: Sequence[TriagedPost]) -> int: ...

    def list_triage_results(self) -> list[TriagedPost]: ...

    def add_feedback(self, feedback: Sequence[Feedback]) -> int: ...

    def list_feedback(self) -> list[Feedback]: ...


@dataclass(frozen=True)
class LegacyPost:
    """Um post do FacebookProspector, já sem os campos de autor.

    É o formato neutro entre o banco antigo e a importação: quem lê o SQLite
    traduz para isto, e `flows/import_legacy.py` traduz disto para o `core`.
    """

    legacy_id: int
    text: str | None
    image_url: str | None
    url: str | None
    posted_at: datetime | None
    collected_at: datetime
    queries: frozenset[str]
    """Todas as buscas que trouxeram o post (`search_query` + `post_search_matches`)."""

    status: str
    """`processing_status` antigo: `QUEUED`, `FILTERED` ou `NO_DATE`."""

    filter_reason: str | None
    vehicle: str | None
    origin: str | None
    destination: str | None
    user_action: str | None
    """`CHAMEI`, `NAO_CHAMEI` ou nada. `PULAR` era adiamento, não decisão."""

    user_action_at: datetime | None
    user_action_reason: str | None

    query_first_seen: Mapping[str, datetime] = field(default_factory=dict)
    """Quando cada busca trouxe o post pela primeira vez. Sem registro, vale `collected_at`.

    Uma busca criada depois pode reencontrar um post antigo; esse reencontro
    tem a data dele, não a do post.
    """


class LegacySource(Protocol):
    """O histórico do FacebookProspector."""

    def read_posts(self) -> list[LegacyPost]: ...
