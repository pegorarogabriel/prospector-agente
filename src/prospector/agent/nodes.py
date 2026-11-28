"""Os nós do grafo semanal.

Ver `docs/arquitetura.md` seção 6. Cada nó é uma função que recebe o estado e
devolve só as chaves que mudou; o LangGraph junta. Os nós são criados por
`make_nodes(deps)` para levarem as dependências junto (closure), sem que elas
entrem no estado nem no checkpoint.

Só dois nós chamam o LLM: `analisar_motivos` e `propor_ajustes`. O resto é
código, e é o código que decide o que entra (`validar_ajustes`).
"""

from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError

from prospector.agent.deps import WeeklyDeps
from prospector.agent.state import WeeklyState
from prospector.agent.tools import Proposal, ProposalRecorder, build_tools
from prospector.core.feedback import FeedbackSource
from prospector.core.weekly import (
    MAX_NEW_RECIPES,
    MAX_RULE_CHANGES,
    ChangeKind,
    RunStatus,
)
from prospector.flows.collect import current_silent_weeks
from prospector.flows.weekly import (
    Adjustment,
    ReviewItem,
    ScoreReport,
    apply_adjustments,
    audit_answered,
    build_summary,
    edit_rules,
    freeze_week,
    had_feedback,
    pick_audit,
    send_audit,
    update_scores,
    week_numbers,
    week_review,
)

Node = Callable[[WeeklyState], dict[str, Any]]

OPERATION = """A operação é uma transportadora com caminhões truck e toco, com baú, baseada em \
Guarulhos (SP). Ela procura empresas que queiram agregar esses caminhões ou que tenham carga \
para eles. O caminhão sai de Guarulhos e volta para Guarulhos: a origem precisa ficar a até \
uns 30 km de Guarulhos; o destino pode ser longe, desde que dentro do estado de São Paulo.

Todo dia, posts públicos do Facebook são coletados com termos de busca (receitas), passam por \
regras fixas e depois por um juiz (um LLM) que lê regras de triagem em linguagem natural. Os \
aprovados vão para o celular do dono, que responde ✓ ou ✗ com um motivo. Toda semana, alguns \
descartados vão para a auditoria ("descartei, confirma?")."""

ANALYZE_SYSTEM = f"""{OPERATION}

Sua tarefa agora: ler as respostas do dono nesta semana e agrupar os motivos em padrões. Um \
padrão é um tipo de erro que se repete (ex.: "aprovou oferta de carreta", "descartou convite \
de grupo que ele queria"). Ignore casos isolados que não ensinam nada. Entregue o resultado \
chamando a ferramenta registrar_padroes, uma vez."""

PROPOSE_SYSTEM = f"""{OPERATION}

Sua tarefa agora: propor ajustes que corrijam os padrões de erro da semana. Você pode propor:
- regra de triagem nova, desligar uma regra, ou trocar o texto de uma regra (o juiz lê essas \
regras antes de cada post);
- um termo de busca novo, que entra como candidato e só vira ativo quando abrir vaga.

O código aplica os guardrails, não você. Ele só aceita:
- até {MAX_RULE_CHANGES} mudanças de regra e {MAX_NEW_RECIPES} busca nova por semana;
- mudança de regra cujo backtest não piore o acerto nem faça o juiz perder mais de 1 post \
que o dono marcou como bom.
Por isso, teste toda mudança de regra com testar_regra antes de propor. Você não pode mexer \
em placar, orçamento nem no status das receitas.

Seja conservador: se os padrões não justificam mudança, proponha nada. Regras curtas e \
concretas funcionam melhor que regras longas. Escreva as regras em português, no mesmo estilo \
das atuais. No fim, chame registrar_propostas uma vez, mesmo com a lista vazia."""


class Pattern(BaseModel):
    description: str = Field(description="O padrão, em uma frase.")
    count: int = Field(description="Quantas respostas da semana mostram o padrão.")
    post_ids: list[str] = Field(default_factory=list, description="Ids de exemplo.")
    direction: str = Field(
        description="'aprovou_ruim' (juiz deixou passar lixo) ou 'descartou_bom' (perdeu post bom)."
    )


class PatternReport(BaseModel):
    patterns: list[Pattern] = Field(description="Os padrões; lista vazia se não houver.")
    summary: str = Field(description="Uma frase sobre a semana, para o resumo do dono.")


def _now_and_start(deps: WeeklyDeps, state: WeeklyState) -> tuple[datetime, datetime]:
    started = datetime.fromisoformat(state["started_at"])
    return deps.now(), started - timedelta(days=7)


def _review_text(items: list[ReviewItem]) -> str:
    lines = []
    for item in items:
        if item.question == "aprovado":
            verdict = "acertou" if item.agent_was_right else "ERROU (era ruim)"
            head = f"Aprovado pela triagem e o dono disse que {verdict}"
        else:
            verdict = "acertou" if item.agent_was_right else "ERROU (era bom)"
            head = f"Descartado pela triagem; na auditoria o dono disse que {verdict}"
        lines.append(
            f"[{item.post_id}] {head}.\n  Motivo da triagem: {item.triage_reason}\n"
            f"  Motivo do dono: {item.my_reason or '-'}\n  Post: {item.snippet}"
        )
    return "\n".join(lines)


def _rules_text(deps: WeeklyDeps) -> str:
    rules = [rule for rule in deps.repository.list_triage_rules() if rule.active]
    return "\n".join(f"[{rule.id}] {rule.text}" for rule in rules) or "(nenhuma)"


def _proposal_label(proposal: Proposal) -> str:
    if proposal.kind == "add_recipe":
        return f'busca "{proposal.query}"'
    if proposal.kind == "deactivate_rule":
        return f"desligar {proposal.rule_id}"
    if proposal.kind == "replace_rule":
        return f'trocar {proposal.rule_id} por "{proposal.text}"'
    return f'regra "{proposal.text}"'


def run_tool_loop(deps: WeeklyDeps, system: str, prompt: str, recorder: ProposalRecorder) -> None:
    """O laço do `propor_ajustes`: LLM → ferramentas → LLM, até registrar as propostas.

    É um grafo pequeno, separado do grafo da semana e sem checkpoint: as
    mensagens (que trazem trechos de posts) vivem só durante o laço. Para o
    grafo da semana, `propor_ajustes` é um nó comum que devolve as propostas.
    """
    tools = build_tools(deps, recorder)
    model = deps.model.bind_tools(tools)

    def call_model(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [model.invoke([SystemMessage(system), *state["messages"]])]}

    def after_model(state: MessagesState) -> str:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    def after_tools(_: MessagesState) -> str:
        return END if recorder.done else "model"

    loop = StateGraph(MessagesState)
    loop.add_node("model", call_model)
    loop.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    loop.add_edge(START, "model")
    loop.add_conditional_edges("model", after_model, ["tools", END])
    loop.add_conditional_edges("tools", after_tools, ["model", END])
    loop.compile().invoke(
        {"messages": [HumanMessage(prompt)]}, {"recursion_limit": deps.max_agent_steps}
    )


def make_nodes(deps: WeeklyDeps) -> dict[str, Node]:
    repo = deps.repository

    def preparar_semana(state: WeeklyState) -> dict[str, Any]:
        now, start = _now_and_start(deps, state)
        ids = pick_audit(repo, start=start, end=now, rng=deps.rng, size=deps.settings.audit_size)
        return {"audit_post_ids": ids}

    def enviar_auditoria(state: WeeklyState) -> dict[str, Any]:
        # Reexecutar o nó (processo caiu depois de enviar) não manda de novo.
        already = repo.notified_post_ids(FeedbackSource.AUDIT)
        pending = [pid for pid in state.get("audit_post_ids", []) if pid not in already]
        report = send_audit(
            repo, deps.messenger, pending, now=deps.now(), image_link=deps.image_link
        )
        deps.commit()
        failed = {post_id for post_id, _ in report.failed}
        return {"audit_post_ids": [p for p in state.get("audit_post_ids", []) if p not in failed]}

    def aguardar_respostas(state: WeeklyState) -> dict[str, Any]:
        ids = state.get("audit_post_ids", [])
        if ids and not audit_answered(repo, ids):
            # Pausa aqui. O processo termina, o checkpoint fica no banco, e a rotina
            # de respostas retoma com Command(resume=...). O valor volta aqui.
            resumed_by = interrupt({"week_id": state["week_id"], "audit_post_ids": ids})
        else:
            resumed_by = "auditoria respondida" if ids else "sem auditoria"
        now, start = _now_and_start(deps, state)
        return {
            "resumed_by": str(resumed_by),
            "had_feedback": had_feedback(repo, start=start, end=now),
            "silent_weeks": current_silent_weeks(repo, now=now),
        }

    def congelar_semana(state: WeeklyState) -> dict[str, Any]:
        report = freeze_week(repo, week_id=state["week_id"], now=deps.now())
        deps.commit()
        return {"score": asdict(report)}

    def atualizar_placares(state: WeeklyState) -> dict[str, Any]:
        report = update_scores(
            repo,
            week_id=state["week_id"],
            now=deps.now(),
            rng=deps.rng,
            settings=deps.settings.scoring,
        )
        deps.commit()
        return {"score": asdict(report)}

    def analisar_motivos(state: WeeklyState) -> dict[str, Any]:
        now, start = _now_and_start(deps, state)
        items = [
            item
            for item in week_review(repo, start=start, end=now)
            if item.my_reason or not item.agent_was_right
        ]
        if not items:
            return {"patterns": [], "analysis": ""}
        tool = StructuredTool.from_function(
            lambda patterns, summary: "ok",
            name="registrar_padroes",
            description="Entrega os padrões encontrados.",
            args_schema=PatternReport,
        )
        prompt = (
            f"Regras de triagem ativas:\n{_rules_text(deps)}\n\n"
            f"Respostas do dono nesta semana:\n{_review_text(items)}"
        )
        try:
            reply = deps.model.bind_tools([tool]).invoke(
                [SystemMessage(ANALYZE_SYSTEM), HumanMessage(prompt)]
            )
        except Exception as error:
            return {"patterns": [], "analysis": "", "agent_error": f"{type(error).__name__}"}
        call = next((c for c in reply.tool_calls if c["name"] == "registrar_padroes"), None)
        if call is None:
            # Respondeu em texto em vez de chamar a ferramenta: vira um padrão só.
            text = reply.text.strip()
            patterns = [{"description": text, "count": len(items), "post_ids": []}] if text else []
            return {"patterns": patterns, "analysis": text[:300]}
        try:
            report = PatternReport.model_validate(call["args"])
        except ValidationError:
            return {"patterns": [], "analysis": "", "agent_error": "padrões fora do formato"}
        return {
            "patterns": [pattern.model_dump() for pattern in report.patterns],
            "analysis": report.summary,
        }

    def propor_ajustes(state: WeeklyState) -> dict[str, Any]:
        patterns = state.get("patterns", [])
        if not patterns:
            return {"proposals": []}
        recorder = ProposalRecorder()
        listed = "\n".join(
            f"- {p['description']} ({p.get('count', '?')}x; exemplos: "
            f"{', '.join(p.get('post_ids', [])) or '-'})"
            for p in patterns
        )
        prompt = (
            f"Padrões de erro desta semana:\n{listed}\n\n"
            f"Regras de triagem ativas:\n{_rules_text(deps)}\n\n"
            "Use as ferramentas para investigar e testar. Termine com registrar_propostas."
        )
        try:
            run_tool_loop(deps, PROPOSE_SYSTEM, prompt, recorder)
        except GraphRecursionError:
            return {"proposals": [], "agent_error": "passou do limite de passos"}
        except Exception as error:
            return {"proposals": [], "agent_error": f"{type(error).__name__}"}
        proposals = [
            p if isinstance(p, Proposal) else Proposal.model_validate(p) for p in recorder.proposals
        ]
        update: dict[str, Any] = {"proposals": [p.model_dump() for p in proposals]}
        if recorder.summary and not state.get("analysis"):
            update["analysis"] = recorder.summary
        return update

    def validar_ajustes(state: WeeklyState) -> dict[str, Any]:
        """Os guardrails, em código. Cada proposta entra ou é barrada com o motivo."""
        rules = repo.list_triage_rules()
        active_ids = {rule.id for rule in rules if rule.active}
        queries = {recipe.query.casefold().strip() for recipe in repo.list_recipes()}
        recipe_ids = {recipe.id for recipe in repo.list_recipes()}
        accepted: list[Adjustment] = []
        rejected: list[str] = []
        rule_changes = new_recipes = 0
        for raw in state.get("proposals", []):
            proposal = Proposal.model_validate(raw)
            label = _proposal_label(proposal)

            def reject(why: str, label: str = label) -> None:
                rejected.append(f"{label}: {why}")

            if proposal.kind == "add_recipe":
                query = (proposal.query or "").strip()
                if new_recipes >= MAX_NEW_RECIPES:
                    reject(f"limite de {MAX_NEW_RECIPES} busca nova por semana")
                elif not query:
                    reject("termo vazio")
                elif query.casefold() in queries:
                    reject("termo já existe")
                else:
                    parent = proposal.parent_id if proposal.parent_id in recipe_ids else None
                    accepted.append(
                        Adjustment(
                            ChangeKind.ADD_RECIPE, proposal.reason, query=query, parent_id=parent
                        )
                    )
                    queries.add(query.casefold())
                    new_recipes += 1
                continue

            if rule_changes >= MAX_RULE_CHANGES:
                reject(f"limite de {MAX_RULE_CHANGES} mudanças de regra por semana")
                continue
            text = (proposal.text or "").strip()
            needs_id = proposal.kind in ("deactivate_rule", "replace_rule")
            needs_text = proposal.kind in ("add_rule", "replace_rule")
            if needs_id and proposal.rule_id not in active_ids:
                reject("regra ativa não encontrada")
                continue
            if needs_text and not text:
                reject("texto vazio")
                continue
            add = [text] if needs_text else []
            off = [proposal.rule_id] if needs_id and proposal.rule_id else []
            candidate = edit_rules(rules, add=add, deactivate=off)
            comparison = deps.tester().compare(rules, candidate)
            if comparison.rejection:
                reject(comparison.rejection)
                continue
            for rule_id in off:
                accepted.append(
                    Adjustment(ChangeKind.DEACTIVATE_RULE, proposal.reason, rule_id=rule_id)
                )
                active_ids.discard(rule_id)
            if add:
                accepted.append(Adjustment(ChangeKind.ADD_RULE, proposal.reason, text=text))
            rules = candidate
            rule_changes += 1
        return {"adjustments": [asdict(item) for item in accepted], "rejected": rejected}

    def aplicar_ajustes(state: WeeklyState) -> dict[str, Any]:
        adjustments = [
            Adjustment(**{**item, "kind": ChangeKind(item["kind"])})
            for item in state.get("adjustments", [])
        ]
        if adjustments:
            apply_adjustments(repo, adjustments, week_id=state["week_id"], now=deps.now())
            deps.commit()
        return {}

    def enviar_resumo(state: WeeklyState) -> dict[str, Any]:
        now, start = _now_and_start(deps, state)
        week_id = state["week_id"]
        score = ScoreReport(**state.get("score", {}))
        text = build_summary(
            week_id=week_id,
            numbers=week_numbers(repo, start=start, end=now),
            frozen=not state.get("had_feedback", False),
            silent_weeks=state.get("silent_weeks", 0),
            score=score,
            changes=repo.list_changes(week_id),
            rejected=state.get("rejected", []),
            analysis=state.get("analysis", ""),
            agent_error=state.get("agent_error"),
        )
        run = repo.get_weekly_run(week_id)
        try:
            sent = deps.messenger.send_text(text)
        except Exception:
            sent = None
        if run is not None:
            repo.update_weekly_run(
                replace(
                    run,
                    status=RunStatus.DONE,
                    finished_at=now,
                    summary_chat_id=sent.chat_id if sent else None,
                    summary_message_id=sent.message_id if sent else None,
                )
            )
        deps.commit()
        return {}

    return {
        "preparar_semana": preparar_semana,
        "enviar_auditoria": enviar_auditoria,
        "aguardar_respostas": aguardar_respostas,
        "congelar_semana": congelar_semana,
        "atualizar_placares": atualizar_placares,
        "analisar_motivos": analisar_motivos,
        "propor_ajustes": propor_ajustes,
        "validar_ajustes": validar_ajustes,
        "aplicar_ajustes": aplicar_ajustes,
        "enviar_resumo": enviar_resumo,
    }
