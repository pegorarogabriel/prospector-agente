"""O juiz LLM: Claude lê o post que passou nas regras e decide.

Ver `docs/arquitetura.md` seção 5. Usa o SDK oficial da Anthropic: o juiz é
uma chamada só, com saída estruturada (`messages.parse` valida a resposta
contra `JudgeOutput`). Sem ferramentas e sem loop: não precisa de LangChain.

O prompt tem duas partes:

- o `system`, estável: papel, regras de triagem e exemplos reais. Ele leva
  `cache_control`, então a partir do segundo post do dia é lido do cache, a
  um décimo do preço;
- a mensagem do usuário, que muda a cada post: o texto, a imagem (por URL) e
  o que as regras determinísticas já perceberam.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

import anthropic
from pydantic import BaseModel, Field

from prospector.core.post import Post, TriageResult
from prospector.core.region import RegionStatus
from prospector.core.rules import RuleVerdict
from prospector.core.triage import FewShotExample, TriageRule

#: Modelo pequeno e barato: classificação de ~30 posts por dia.
DEFAULT_MODEL = "claude-haiku-5-5"

#: Preço por milhão de tokens (entrada, saída). Cache: escrita 1,25x, leitura 0,1x.
PRICES_PER_MILLION = {"claude-haiku-5-5": (0.10, 0.50)}

#: A resposta é um JSON curto; isto só evita corte no meio.
MAX_TOKENS = 2048

ROLE = """Você é o juiz de uma prospecção de fretes. A operação é uma transportadora com \
caminhões truck e toco, com baú, baseada em Guarulhos (SP). Ela procura empresas que \
queiram agregar esses caminhões ou que tenham carga para eles.

Limitação da operação: o caminhão sai de Guarulhos e volta para Guarulhos. Não compensa \
deslocar o caminhão para operar em outra cidade. Então:
- a origem (onde o caminhão carrega ou se apresenta) precisa ficar a até uns 30 km de \
Guarulhos: Guarulhos, Arujá, Itaquaquecetuba, Poá, Mairiporã, zonas Norte e Leste de São \
Paulo, e no limite a capital, o ABC e Osasco. Operação baseada em Sorocaba, Campinas, \
Sumaré, Jundiaí ou Mogi das Cruzes não serve, mesmo sendo perto;
- o destino pode ser longe, desde que dentro do estado de São Paulo;
- fora do estado de São Paulo não serve.
Não deduza a região pelo DDD do telefone: use só as cidades citadas no post.

Você recebe posts públicos do Facebook que já passaram por um filtro automático \
(idioma, venda óbvia, veículo incompatível, região). Decida se o dono da operação \
deveria entrar em contato com quem postou.

Responda com:
- approved: true se vale o contato;
- score: de 0 a 1, a sua confiança de que vale o contato. Ela ordena o que chega \
ao celular, então use a escala inteira;
- reason: uma frase em português, direta, explicando a decisão. É o que aparece \
na notificação;
- vehicle, origin, destination: o que o post disser; null se não disser.

Quando o post for só imagem e você não conseguir ver a imagem, reprove com nota \
baixa e diga isso no motivo."""


class JudgeOutput(BaseModel):
    """O formato que o Claude é obrigado a devolver."""

    approved: bool
    score: float = Field(description="Confiança de 0 a 1 de que vale o contato.")
    reason: str = Field(description="Uma frase em português explicando a decisão.")
    vehicle: str | None = None
    origin: str | None = None
    destination: str | None = None


@dataclass
class UsageTotals:
    """Tokens gastos pelo juiz, para o relatório de custo."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, usage: object) -> None:
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    def cost_usd(self, model: str) -> float | None:
        prices = PRICES_PER_MILLION.get(model)
        if prices is None:
            return None
        input_price, output_price = prices
        return (
            self.input_tokens * input_price
            + self.cache_write_tokens * input_price * 1.25
            + self.cache_read_tokens * input_price * 0.1
            + self.output_tokens * output_price
        ) / 1_000_000


def build_system_prompt(rules: Sequence[TriageRule], examples: Sequence[FewShotExample]) -> str:
    """O `system` do juiz: papel, regras ativas e exemplos reais."""
    parts = [ROLE, "", "## Regras de triagem", ""]
    active = [rule for rule in rules if rule.active]
    parts += [f"{number}. {rule.text}" for number, rule in enumerate(active, start=1)]
    if examples:
        parts += ["", "## Exemplos de decisões do dono da operação", ""]
        for example in examples:
            verdict = "VALE O CONTATO" if example.good else "NÃO VALE"
            parts += [
                "<exemplo>",
                example.post_text.strip(),
                f"Decisão: {verdict}. Motivo dele: {example.reason.strip()}",
                "</exemplo>",
                "",
            ]
    return "\n".join(parts).strip()


def _signals(verdict: RuleVerdict) -> str:
    """O que as regras determinísticas já perceberam no post."""
    lines = []
    if verdict.vehicle:
        lines.append(f"- veículo citado: {verdict.vehicle}")
    if verdict.body:
        lines.append(f"- carroceria citada: {verdict.body}")
    if verdict.mixed:
        lines.append("- cita veículo ou carroceria que servem e que não servem")
    if verdict.region.status is RegionStatus.INSIDE:
        lines.append(f"- região: dentro do raio ({verdict.region.detail})")
    if verdict.driver_offering:
        lines.append("- parece motorista se oferecendo, e não empresa com carga")
    return "\n".join(lines) if lines else "- nada de especial"


def build_user_content(post: Post, verdict: RuleVerdict, *, with_image: bool) -> list[dict]:
    """A mensagem do usuário: imagem (se houver) antes do texto, como pede a API."""
    content: list[dict] = []
    if with_image and post.image_url:
        content.append({"type": "image", "source": {"type": "url", "url": post.image_url}})
    text = post.text.strip() or "(o post não tem texto; o conteúdo está na imagem)"
    if post.image_url and not with_image:
        text += "\n(havia uma imagem, mas ela não pôde ser carregada)"
    content.append(
        {
            "type": "text",
            "text": f"<post>\n{text}\n</post>\n\nSinais do filtro automático:\n{_signals(verdict)}",
        }
    )
    return content


def _clamp(score: float) -> float:
    return min(max(float(score), 0.0), 1.0)


@dataclass
class ClaudeJudge:
    """Implementa a porta `Judge` com Claude.

    Uma falha num post não pode parar a coleta: recusa ou erro da API viram
    reprovação com o motivo, e a coleta segue. Os retries de 429 e 5xx são do
    próprio SDK.
    """

    client: anthropic.Anthropic
    rules: Sequence[TriageRule]
    examples: Sequence[FewShotExample] = ()
    model: str = DEFAULT_MODEL
    usage: UsageTotals = field(default_factory=UsageTotals)

    def __post_init__(self) -> None:
        self._system = [
            {
                "type": "text",
                "text": build_system_prompt(self.rules, self.examples),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    @property
    def system_prompt(self) -> str:
        return self._system[0]["text"]

    def _ask(self, post: Post, verdict: RuleVerdict, *, with_image: bool) -> object:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=self._system,
            messages=[
                {
                    "role": "user",
                    "content": build_user_content(post, verdict, with_image=with_image),
                }
            ],
            output_format=JudgeOutput,
            output_config={"effort": "low"},
        )
        self.usage.add(response.usage)
        return response

    def judge(self, post: Post, verdict: RuleVerdict) -> TriageResult:
        try:
            try:
                response = self._ask(post, verdict, with_image=bool(post.image_url))
            except anthropic.BadRequestError:
                if not post.image_url:
                    raise
                # A URL de imagem do Facebook expira ou bloqueia download: tenta sem ela.
                response = self._ask(post, verdict, with_image=False)
        except anthropic.APIError as error:
            return self._unavailable(f"{type(error).__name__}")

        if getattr(response, "stop_reason", None) == "refusal":
            return self._unavailable("recusou o post")
        output = getattr(response, "parsed_output", None)
        if not isinstance(output, JudgeOutput):
            return self._unavailable("resposta fora do formato")
        return TriageResult(
            approved=output.approved,
            score=_clamp(output.score),
            reason=output.reason.strip() or "sem motivo",
            vehicle=output.vehicle or verdict.vehicle,
            origin=output.origin or verdict.region.origin,
            destination=output.destination or verdict.region.destination,
        )

    @staticmethod
    def _unavailable(why: str) -> TriageResult:
        return TriageResult(approved=False, score=0.0, reason=f"juiz indisponível: {why}")
