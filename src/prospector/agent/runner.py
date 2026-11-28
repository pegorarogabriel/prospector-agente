"""Começar e retomar a semana. É o que os comandos `prospector weekly ...` chamam.

- `start_week` roda no domingo (`weekly.yml`): registra a semana e roda o grafo
  até a pausa da auditoria (ou até o fim, se não houver o que auditar).
- `resume_week` roda a cada 2h junto com a leitura das respostas
  (`replies.yml`): se a semana está pausada e a auditoria foi respondida, ou o
  prazo de segunda 8h venceu, retoma o grafo de onde parou.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from prospector.agent.deps import WeeklyDeps
from prospector.agent.graph import build_graph
from prospector.core.weekly import RunStatus, WeeklyRun, should_resume, week_id_for
from prospector.flows.weekly import audit_answered


@dataclass(frozen=True)
class RunReport:
    week_id: str | None
    status: str
    """Em português, para o log: o que aconteceu nesta chamada."""

    waiting: bool = False


def _config(week_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": f"weekly-{week_id}"}}


def _finished(deps: WeeklyDeps, checkpointer: BaseCheckpointSaver, week_id: str) -> RunReport:
    snapshot = build_graph(deps, checkpointer).get_state(_config(week_id))
    if snapshot.next:
        return RunReport(week_id, "pausado esperando a auditoria", waiting=True)
    return RunReport(week_id, "semana concluída, resumo enviado")


def start_week(
    deps: WeeklyDeps, checkpointer: BaseCheckpointSaver, *, now: datetime
) -> list[RunReport]:
    """Começa a semana de `now`. Se a anterior ficou pausada, termina ela antes."""
    reports: list[RunReport] = []
    week_id = week_id_for(now)
    pending = deps.repository.pending_weekly_run()
    if pending is not None and pending.week_id != week_id:
        reports.append(resume_week(deps, checkpointer, now=now, force=True))

    if deps.repository.get_weekly_run(week_id) is not None:
        reports.append(RunReport(week_id, "semana já iniciada antes; nada a fazer"))
        return reports

    run = WeeklyRun(week_id, now, now + deps.settings.audit_wait)
    deps.repository.add_weekly_run(run)
    deps.commit()
    graph = build_graph(deps, checkpointer)
    graph.invoke({"week_id": week_id, "started_at": now.isoformat()}, _config(week_id))
    reports.append(_finished(deps, checkpointer, week_id))
    return reports


def resume_week(
    deps: WeeklyDeps, checkpointer: BaseCheckpointSaver, *, now: datetime, force: bool = False
) -> RunReport:
    """Retoma a semana pausada, se for a hora. `force` ignora o prazo."""
    run = deps.repository.pending_weekly_run()
    if run is None:
        return RunReport(None, "nenhuma semana pausada")

    graph = build_graph(deps, checkpointer)
    snapshot = graph.get_state(_config(run.week_id))
    if not snapshot.next:
        # Sem checkpoint (ou já no fim): não há de onde retomar. Fecha a semana
        # para ela não travar as próximas.
        deps.repository.update_weekly_run(replace(run, status=RunStatus.DONE, finished_at=now))
        deps.commit()
        return RunReport(run.week_id, "checkpoint não encontrado; semana encerrada sem resumo")

    ids = list(snapshot.values.get("audit_post_ids", []))
    answered = audit_answered(deps.repository, ids)
    if not force and not should_resume(
        audit_answered=answered, now=now, resume_after=run.resume_after
    ):
        return RunReport(run.week_id, "esperando a auditoria", waiting=True)

    reason = "auditoria respondida" if answered else "prazo da auditoria vencido"
    graph.invoke(Command(resume=reason), _config(run.week_id))
    return _finished(deps, checkpointer, run.week_id)
