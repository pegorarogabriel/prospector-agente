"""O que o juiz recebe além do post: regras em linguagem natural e exemplos.

Ver `docs/arquitetura.md` seção 5. As regras ficam no banco (`triage_rules`)
porque é o agente semanal quem as ajusta; os exemplos são minhas respostas
reais, com o motivo que eu dei.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TriageRule:
    """Uma regra de triagem em português, como o juiz vai lê-la."""

    id: str
    text: str
    active: bool = True
    created_by: str = "manual"
    """`seed` (regra inicial), `agent` (proposta pelo agente semanal) ou `manual`."""


@dataclass(frozen=True)
class FewShotExample:
    """Um post que eu julguei, com o motivo. Vira exemplo no prompt do juiz."""

    post_id: str
    post_text: str
    good: bool
    """O post era bom? (Eu teria chamado.)"""

    reason: str
    answered_at: datetime


def select_examples(
    labeled: Sequence[FewShotExample],
    *,
    good: int,
    bad: int,
    exclude_ids: frozenset[str] = frozenset(),
) -> list[FewShotExample]:
    """Os exemplos mais recentes com motivo e com texto, equilibrando bons e ruins.

    Equilibrar importa: no histórico há 105 "não chamei" para 19 "chamei". Com
    exemplos na mesma proporção, o juiz aprenderia que quase tudo é ruim.

    `exclude_ids` existe para o backtest: o post sendo medido não pode estar
    entre os exemplos, senão o juiz "cola" a resposta.
    """
    usable = [
        example
        for example in labeled
        if example.reason.strip()
        and example.post_text.strip()
        and example.post_id not in exclude_ids
    ]
    newest_first = sorted(usable, key=lambda example: example.answered_at, reverse=True)
    chosen = [example for example in newest_first if example.good][:good]
    chosen += [example for example in newest_first if not example.good][:bad]
    return sorted(chosen, key=lambda example: example.answered_at)
