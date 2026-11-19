"""Receita de busca: o termo que vai ao Apify e o placar que diz se ele presta.

Ver `docs/arquitetura.md` seção 3.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

#: Máximo de receitas ativas ao mesmo tempo (arquitetura seção 3).
MAX_ACTIVE_RECIPES = 4


class RecipeStatus(StrEnum):
    """Onde a receita está no ciclo de vida."""

    ACTIVE = "active"
    """Disputa o orçamento diário no sorteio."""

    CANDIDATE = "candidate"
    """Criada pelo agente semanal; espera uma vaga aberta por uma aposentadoria."""

    RETIRED = "retired"
    """Perdeu a disputa de forma estatisticamente convincente. Não volta sozinha."""


class RecipeOrigin(StrEnum):
    """Quem criou a receita. Serve para auditar de onde vieram os bons termos."""

    LEGACY = "legacy"
    """Importada do FacebookProspector."""

    AGENT = "agent"
    """Proposta pelo agente semanal."""

    MANUAL = "manual"
    """Escrita à mão."""


@dataclass(frozen=True)
class Recipe:
    """Um termo de busca com o placar de acertos e erros que ele acumulou.

    O placar é uma distribuição Beta. `successes` e `failures` são as observações
    e `alpha`/`beta` somam o prior `Beta(1, 1)` — uma receita nova começa como
    "não sei nada sobre ela", não como "ela é ruim".

    São floats, e não inteiros, porque o esquecimento semanal multiplica os dois
    por `DECAY` (ver `bandit.decay`).

    Não use instâncias como chave de dicionário: `params` é um mapa e a classe,
    embora congelada, não é hasheável de fato.
    """

    id: str
    query: str
    """O termo de busca, ex.: "agregado truck"."""

    params: Mapping[str, str] = field(default_factory=dict)
    """Parâmetros do actor do Apify, ex.: `{"searchType": "latest"}`."""

    successes: float = 0.0
    failures: float = 0.0
    status: RecipeStatus = RecipeStatus.CANDIDATE
    origin: RecipeOrigin = RecipeOrigin.MANUAL

    parent_id: str | None = None
    """De qual receita esta derivou, quando o agente a criou variando outra."""

    @property
    def alpha(self) -> float:
        """Parâmetro alpha da Beta: acertos + o prior."""
        return self.successes + 1.0

    @property
    def beta(self) -> float:
        """Parâmetro beta da Beta: erros + o prior."""
        return self.failures + 1.0

    @property
    def trials(self) -> float:
        """Observações acumuladas, já descontado o esquecimento."""
        return self.successes + self.failures

    @property
    def expected_rate(self) -> float:
        """Taxa de acerto esperada, a média da Beta.

        Uma receita sem nenhuma observação vale 0.5: incerteza, não pessimismo.
        """
        return self.alpha / (self.alpha + self.beta)

    def with_outcome(self, *, success: bool) -> "Recipe":
        """Devolve a receita com uma observação a mais no placar."""
        if success:
            return replace(self, successes=self.successes + 1.0)
        return replace(self, failures=self.failures + 1.0)
