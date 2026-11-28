"""O estado do grafo semanal: o que passa de um nó para o outro.

Ver `docs/arquitetura.md` seção 6. Este dicionário é o que o `PostgresSaver`
grava a cada passo (o checkpoint). É por ele que o grafo pausa no domingo e
retoma na segunda, em outro processo, como se nada tivesse acontecido.

Duas regras para ele:

- **Só tipos simples** (texto, número, lista, dicionário). Datas vão como
  texto ISO. Assim o checkpoint é legível e não depende de como cada classe
  é serializada.
- **Nada de texto de post.** O estado guarda ids, contagens e o que o LLM
  concluiu. O texto dos posts fica no banco, onde já está protegido.
"""

from typing import Any, TypedDict


class WeeklyState(TypedDict, total=False):
    week_id: str
    """A semana, ex. `2026-11-29`. Também é o `thread_id` do checkpoint."""

    started_at: str
    """Quando a execução começou (ISO). A semana analisada é a anterior a isto."""

    audit_post_ids: list[str]
    resumed_by: str
    """Por que o grafo seguiu: auditoria respondida, prazo vencido ou sem auditoria."""

    had_feedback: bool
    silent_weeks: int
    score: dict[str, Any]
    patterns: list[dict[str, Any]]
    analysis: str
    proposals: list[dict[str, Any]]
    adjustments: list[dict[str, Any]]
    rejected: list[str]
    agent_error: str
