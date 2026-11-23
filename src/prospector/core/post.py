"""O post coletado e o veredito da triagem sobre ele.

Ver `docs/arquitetura.md` seção 5.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Post:
    """Um post público coletado do Facebook.

    `recipe_ids` é um conjunto porque o mesmo post pode ser encontrado por mais
    de uma receita — e, quando é, conta como tentativa para todas elas
    (arquitetura seção 3).

    Campos de autor (nome, perfil) não entram aqui: não são necessários para
    aprender, e o repositório é público.
    """

    id: str
    text: str
    url: str
    posted_at: datetime
    location: str | None = None
    recipe_ids: frozenset[str] = field(default_factory=frozenset)

    def age(self, now: datetime) -> timedelta:
        """Quanto tempo faz que o post foi publicado."""
        return now - self.posted_at


@dataclass(frozen=True)
class TriageResult:
    """A decisão da triagem sobre um post, com o motivo em linguagem natural.

    `reason` não é enfeite: é o que vai na mensagem do Telegram para eu julgar
    a decisão do agente, e o que alimenta `analisar_motivos` no agente semanal.
    """

    approved: bool
    score: float
    """Confiança entre 0 e 1. Ordena quais aprovados vão para o Telegram."""

    reason: str
    vehicle: str | None = None
    origin: str | None = None
    destination: str | None = None
