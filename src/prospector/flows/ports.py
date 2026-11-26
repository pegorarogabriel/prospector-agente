"""Portas: o que os casos de uso precisam do mundo, sem saber quem entrega.

Cada porta é um `Protocol` — uma interface estrutural. Quem a implementa não
precisa herdar dela: basta ter os mesmos métodos. As implementações concretas
moram em `integrations/` (Postgres, SQLite antigo, memória), e os flows só
conhecem estas assinaturas. É isso que deixa os testes rodarem sem banco.

Ver `docs/arquitetura.md` seção 9.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from prospector.core.feedback import Feedback
from prospector.core.notification import Notification, Reply
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe
from prospector.core.rules import RuleVerdict
from prospector.core.triage import TriageRule


@dataclass(frozen=True)
class TriagedPost:
    """A decisão da triagem amarrada ao post sobre o qual ela foi tomada."""

    post_id: str
    result: TriageResult


@dataclass(frozen=True)
class LabeledPost:
    """Um post com a decisão da triagem e a minha resposta sobre ela."""

    post: Post
    decision: TriageResult
    feedback: Feedback


@dataclass(frozen=True)
class SearchRun:
    """Uma chamada ao Apify: quanto foi pedido e quanto foi cobrado.

    É a fonte do "gasto do mês" que a trava de orçamento consulta antes de toda
    busca (seção 7). Registra também os testes `--live` do dry-run: o crédito
    foi consumido, então a trava precisa saber.
    """

    ran_at: datetime
    recipe_id: str
    requested: int
    charged: int
    live: bool
    """`True` para coleta de verdade ou teste `--live`. (Hoje toda linha é live.)"""


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

    def add_triage_rules(self, rules: Sequence[TriageRule]) -> int: ...

    def list_triage_rules(self) -> list[TriageRule]:
        """Todas as regras, ativas ou não, em ordem de id."""
        ...

    def list_labeled_posts(self) -> list[LabeledPost]:
        """Posts que têm decisão e resposta minha: o gabarito do backtest."""
        ...

    def existing_post_ids(self, ids: Sequence[str]) -> set[str]:
        """Quais destes posts já estão no banco (vistos numa coleta anterior)."""
        ...

    def add_search_runs(self, runs: Sequence[SearchRun]) -> int:
        """Registra chamadas ao Apify. São eventos: sempre entram, nunca deduplicam."""
        ...

    def results_spent_since(self, moment: datetime) -> int:
        """Resultados cobrados pelo Apify desde `moment` (o 1º dia do mês, na trava)."""
        ...

    def add_notifications(self, notifications: Sequence[Notification]) -> int:
        """Registra mensagens enviadas. Uma por (chat, mensagem)."""
        ...

    def find_notifications(
        self, chat_id: int, message_ids: Sequence[int]
    ) -> dict[int, Notification]:
        """As notificações destas mensagens, por `message_id`. As desconhecidas ficam de fora."""
        ...

    def reply_offset(self) -> int | None:
        """Até onde as atualizações do Telegram já foram lidas (o próximo `update_id`)."""
        ...

    def save_reply_offset(self, offset: int) -> None:
        """Guarda o próximo `update_id`. Entra na mesma transação das respostas."""
        ...


@dataclass(frozen=True)
class SearchResult:
    """O que uma busca devolveu e quanto ela custou."""

    posts: list[Post]
    charged: int
    """Resultados cobrados. Pode ser maior que `len(posts)`: item sem texto, imagem
    ou data que não vira post também é cobrado."""


class SearchProvider(Protocol):
    """De onde vêm os posts: o Apify, ou posts de exemplo no dry-run."""

    def search(
        self, query: str, params: Mapping[str, str], *, limit: int, since: datetime
    ) -> SearchResult: ...


class ImageStore(Protocol):
    """Onde a cópia das imagens fica guardada (Supabase Storage, ou memória nos testes)."""

    def save(self, post: Post) -> str | None:
        """Copia a imagem do post e devolve o caminho dela, ou `None` se não deu.

        Falhar não é erro da coleta: a imagem pode já ter expirado ou ser
        grande demais. O post segue sem cópia.
        """
        ...

    def link(self, path: str) -> str | None:
        """Uma URL temporária para o juiz ler a cópia. `None` se não deu."""
        ...


class Judge(Protocol):
    """A camada 2 da triagem: lê o post que passou nas regras e decide."""

    def judge(self, post: Post, verdict: RuleVerdict) -> TriageResult: ...


@dataclass(frozen=True)
class PostMessage:
    """O que vai na mensagem de um post aprovado (seção 8). Quem desenha é a integração."""

    post: Post
    result: TriageResult
    queries: tuple[str, ...]
    """Os termos das receitas que trouxeram o post."""

    age: timedelta
    reject_reasons: tuple[str, ...]
    """Os motivos de ✗ que viram botão, nesta ordem."""


@dataclass(frozen=True)
class SentMessage:
    chat_id: int
    message_id: int


@dataclass(frozen=True)
class ReplyBatch:
    """As respostas lidas numa rodada, e de onde ler na próxima."""

    replies: list[Reply]
    next_offset: int | None
    """O `update_id` seguinte ao último lido, inclusive os ignorados. `None` se não veio nada."""


class Messenger(Protocol):
    """A ponte com o celular: o bot do Telegram, ou um fake nos testes."""

    def send_post(self, message: PostMessage) -> SentMessage: ...

    def fetch_replies(self, offset: int | None) -> ReplyBatch:
        """Botões e replies desde `offset`, só do meu chat. Não confirma nada sozinho."""
        ...

    def confirm(self, chat_id: int, message_id: int, label: str) -> None:
        """Troca os botões da mensagem por um só, inerte, com o que foi gravado."""
        ...


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
