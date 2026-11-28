"""Tudo o que o grafo semanal precisa do mundo, entregue pelo `cli.py`.

O grafo não abre conexão nem cria cliente de API: ele recebe as portas
prontas. O checkpoint guarda só o estado; as dependências são montadas de novo
a cada processo (domingo para começar, segunda para retomar).
"""

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from langchain_core.language_models import BaseChatModel

from prospector.flows.ports import JudgeFactory, Messenger, Repository, SearchProvider
from prospector.flows.weekly import RuleTester, WeeklySettings

#: Passos do laço de ferramentas do `propor_ajustes` (cada chamada ao LLM e
#: cada rodada de ferramentas conta um). Passou disso, a semana segue sem ajuste.
MAX_AGENT_STEPS = 40

#: Testes de regra que o LLM pode pedir por semana (cada um roda o juiz até
#: `RULE_TEST_LIMIT` vezes).
MAX_RULE_TESTS = 6


def _no_commit() -> None:
    return None


@dataclass
class WeeklyDeps:
    repository: Repository
    messenger: Messenger
    model: BaseChatModel
    """O LLM do agente. A porta é a interface `BaseChatModel` do LangChain: o
    `cli.py` entrega o Claude (`ChatAnthropic`), os testes um modelo roteirizado."""

    make_judge: JudgeFactory
    search: SearchProvider
    now: Callable[[], datetime]
    rng: random.Random
    commit: Callable[[], None] = _no_commit
    """Confirma no banco o que um nó gravou, antes de o checkpoint dizer que o
    nó terminou. No dry-run, não faz nada."""

    settings: WeeklySettings = field(default_factory=WeeklySettings)
    image_link: Callable[[str], str | None] | None = None
    max_agent_steps: int = MAX_AGENT_STEPS
    max_rule_tests: int = MAX_RULE_TESTS
    _tester: RuleTester | None = field(default=None, init=False, repr=False)

    def tester(self) -> RuleTester:
        """Um testador de regras por processo: as medições são reaproveitadas."""
        if self._tester is None:
            self._tester = RuleTester(
                self.repository.list_labeled_posts(),
                self.make_judge,
                limit=self.settings.rule_test_limit,
                image_link=self.image_link,
            )
        return self._tester
