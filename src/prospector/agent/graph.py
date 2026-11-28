"""O grafo do agente semanal (LangGraph).

Ver o desenho em `docs/arquitetura.md` seção 6:

    preparar_semana → enviar_auditoria → ⏸ aguardar_respostas
        ├─ sem feedback → congelar_semana ────────────────────────┐
        └─ com feedback → atualizar_placares → analisar_motivos   │
                          → propor_ajustes → validar_ajustes      │
                          → aplicar_ajustes → enviar_resumo ◄─────┘

A pausa em `aguardar_respostas` é um `interrupt()`. Com um checkpointer
(`PostgresSaver` em produção), o estado fica gravado no banco e o processo
termina; outro processo, horas depois, retoma de onde parou.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from prospector.agent.deps import WeeklyDeps
from prospector.agent.nodes import make_nodes
from prospector.agent.state import WeeklyState


def route_after_wait(state: WeeklyState) -> str:
    """Semana sem feedback = semana congelada (seção 4)."""
    return "atualizar_placares" if state.get("had_feedback") else "congelar_semana"


def build_graph(deps: WeeklyDeps, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    graph = StateGraph(WeeklyState)
    for name, node in make_nodes(deps).items():
        graph.add_node(name, node)

    graph.add_edge(START, "preparar_semana")
    graph.add_edge("preparar_semana", "enviar_auditoria")
    graph.add_edge("enviar_auditoria", "aguardar_respostas")
    graph.add_conditional_edges(
        "aguardar_respostas", route_after_wait, ["atualizar_placares", "congelar_semana"]
    )
    graph.add_edge("congelar_semana", "enviar_resumo")
    graph.add_edge("atualizar_placares", "analisar_motivos")
    graph.add_edge("analisar_motivos", "propor_ajustes")
    graph.add_edge("propor_ajustes", "validar_ajustes")
    graph.add_edge("validar_ajustes", "aplicar_ajustes")
    graph.add_edge("aplicar_ajustes", "enviar_resumo")
    graph.add_edge("enviar_resumo", END)
    return graph.compile(checkpointer=checkpointer)
