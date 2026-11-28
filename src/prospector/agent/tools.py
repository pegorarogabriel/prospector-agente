"""As ferramentas do LLM no `propor_ajustes`.

Ver `docs/arquitetura.md` seção 6. Cada ferramenta chama um caso de uso de
`flows/` e devolve texto curto para o LLM ler. Nenhuma fala com Apify, banco ou
Telegram direto (regra do CLAUDE.md), e nenhuma grava ajuste: quem grava é o
`aplicar_ajustes`, depois que o código dos guardrails aprovou.

Os limites de uso (buscas de teste, testes de regra) são contados aqui, em
código. O LLM é avisado na resposta da ferramenta quando passa do limite.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from prospector.agent.deps import WeeklyDeps
from prospector.flows.weekly import edit_rules, probe_query, search_history


class Proposal(BaseModel):
    """Um ajuste proposto. O código decide se ele entra."""

    kind: Literal["add_rule", "deactivate_rule", "replace_rule", "add_recipe"] = Field(
        description=(
            "add_rule: regra de triagem nova; deactivate_rule: desligar uma regra; "
            "replace_rule: trocar o texto de uma regra (desliga a antiga, cria a nova); "
            "add_recipe: termo de busca novo, que entra como candidato."
        )
    )
    reason: str = Field(description="Por que, em uma frase, citando os motivos do dono.")
    text: str | None = Field(
        default=None, description="Texto da regra nova (add_rule, replace_rule)."
    )
    rule_id: str | None = Field(
        default=None, description="Id da regra existente (deactivate_rule, replace_rule)."
    )
    query: str | None = Field(default=None, description="Termo de busca novo (add_recipe).")
    parent_id: str | None = Field(
        default=None, description="Receita da qual o termo novo deriva, se houver (add_recipe)."
    )


class RegisterProposals(BaseModel):
    proposals: list[Proposal] = Field(
        description="Os ajustes propostos. Lista vazia se nada vale a pena mudar."
    )
    summary: str = Field(description="Uma ou duas frases para o resumo da semana.")


class HistoryQuery(BaseModel):
    days: int | None = Field(default=None, description="Só respostas dos últimos N dias.")
    recipe_id: str | None = Field(default=None, description="Só posts desta receita.")
    text: str | None = Field(
        default=None, description="Trecho a procurar no motivo do dono, da triagem ou no post."
    )
    good: bool | None = Field(
        default=None, description="true: só posts bons (ele chamaria); false: só ruins."
    )
    limit: int = Field(default=15, description="Quantos posts, no máximo 30.")


class RuleTest(BaseModel):
    add: list[str] = Field(default_factory=list, description="Textos de regras novas.")
    deactivate: list[str] = Field(default_factory=list, description="Ids de regras a desligar.")


class RecipeTest(BaseModel):
    query: str = Field(description="O termo de busca a testar no Facebook.")


@dataclass
class ProposalRecorder:
    """Onde a ferramenta final guarda as propostas, e os contadores de uso."""

    proposals: list[Proposal] = field(default_factory=list)
    summary: str = ""
    done: bool = False
    rule_tests: int = 0
    probes: int = 0


def _pct(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "-"


def build_tools(deps: WeeklyDeps, recorder: ProposalRecorder) -> list[BaseTool]:
    """As ferramentas, amarradas às dependências desta execução."""
    now: datetime = deps.now()

    def consultar_historico(
        days: int | None = None,
        recipe_id: str | None = None,
        text: str | None = None,
        good: bool | None = None,
        limit: int = 15,
    ) -> str:
        items = search_history(
            deps.repository,
            now=now,
            days=days,
            recipe_id=recipe_id,
            text=text,
            good=good,
            limit=limit,
        )
        if not items:
            return "Nenhum post encontrado com esses filtros."
        lines = []
        for item in items:
            label = "BOM" if item.good else "RUIM"
            decision = "aprovado" if item.approved else "descartado"
            lines.append(
                f"[{item.post_id}] {item.answered_at:%d/%m} {label}, triagem {decision} "
                f"({item.triage_reason}); motivo do dono: {item.my_reason or '-'}; "
                f"receitas: {', '.join(sorted(item.recipe_ids)) or '-'}\n  {item.snippet}"
            )
        return "\n".join(lines)

    def listar_receitas() -> str:
        lines = []
        for recipe in deps.repository.list_recipes():
            lines.append(
                f'[{recipe.id}] "{recipe.query}" status={recipe.status.value} '
                f"taxa={recipe.expected_rate:.2f} tentativas={recipe.trials:.0f} "
                f"origem={recipe.origin.value}"
            )
        return "\n".join(lines) or "Nenhuma receita."

    def testar_regra(add: list[str] | None = None, deactivate: list[str] | None = None) -> str:
        if recorder.rule_tests >= deps.max_rule_tests:
            return f"Limite de {deps.max_rule_tests} testes de regra por semana atingido."
        recorder.rule_tests += 1
        current = deps.repository.list_triage_rules()
        known = {rule.id for rule in current if rule.active}
        unknown = [rule_id for rule_id in deactivate or [] if rule_id not in known]
        if unknown:
            return f"Regras ativas desconhecidas: {', '.join(unknown)}"
        candidate = edit_rules(current, add=add or [], deactivate=deactivate or [])
        result = deps.tester().compare(current, candidate)
        verdict = (
            "PASSARIA nos guardrails"
            if result.rejection is None
            else (f"NÃO passaria: {result.rejection}")
        )
        return (
            f"Backtest em {result.measured} posts rotulados pelo dono.\n"
            f"acerto: {_pct(result.before_accuracy)} → {_pct(result.after_accuracy)}\n"
            f"precisão: {_pct(result.before_precision)} → {_pct(result.after_precision)}\n"
            f"recall: {_pct(result.before_recall)} → {_pct(result.after_recall)}\n"
            f"posts bons que deixariam de ser aprovados: {result.good_posts_lost}\n"
            f"{verdict}"
        )

    def testar_receita(query: str) -> str:
        if recorder.probes >= deps.settings.max_probes:
            return f"Limite de {deps.settings.max_probes} buscas de teste por semana atingido."
        recorder.probes += 1
        report = probe_query(deps.repository, deps.search, query, now=now, settings=deps.settings)
        if report.skipped_reason:
            return f"Busca não feita: {report.skipped_reason}."
        lines = [
            f'"{query}": {report.returned} posts devolvidos, {report.kept} passaram nas '
            "regras fixas (idade, veículo, região, venda)."
        ]
        lines += [f"- {snippet}" for snippet in report.snippets]
        return "\n".join(lines)

    def registrar_propostas(proposals: list[Proposal], summary: str) -> str:
        recorder.proposals = list(proposals)
        recorder.summary = summary
        recorder.done = True
        return "Propostas registradas. O código vai validar e aplicar o que passar."

    return [
        StructuredTool.from_function(
            consultar_historico,
            name="consultar_historico",
            description="Busca posts que o dono já julgou (bom/ruim), com filtros.",
            args_schema=HistoryQuery,
        ),
        StructuredTool.from_function(
            listar_receitas,
            name="listar_receitas",
            description="Lista as receitas de busca com placar, taxa estimada e status.",
        ),
        StructuredTool.from_function(
            testar_regra,
            name="testar_regra",
            description=(
                "Roda o juiz com as regras alteradas contra os posts que o dono julgou e "
                "compara com as regras atuais. Use antes de propor qualquer mudança de regra."
            ),
            args_schema=RuleTest,
        ),
        StructuredTool.from_function(
            testar_receita,
            name="testar_receita",
            description=(
                "Faz uma busca de teste no Facebook (5 resultados) para ver se um termo traz "
                "algo útil. Gasta orçamento real: no máximo 2 por semana."
            ),
            args_schema=RecipeTest,
        ),
        StructuredTool.from_function(
            registrar_propostas,
            name="registrar_propostas",
            description=(
                "Entrega as propostas finais e encerra. Chame uma vez só, no fim, mesmo que "
                "a lista seja vazia."
            ),
            args_schema=RegisterProposals,
        ),
    ]
