"""O juiz Claude, com um cliente falso no lugar da API. Nenhum teste chama a Anthropic."""

from datetime import UTC, datetime
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from prospector.core.post import Post
from prospector.core.region import RegionStatus, RegionVerdict
from prospector.core.rules import RuleVerdict
from prospector.core.triage import FewShotExample, TriageRule
from prospector.integrations.llm import ClaudeJudge, JudgeOutput, UsageTotals

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)
RULES = [
    TriageRule("r1", "Aprove empresa com carga para truck baú."),
    TriageRule("r2", "Reprove motorista se oferecendo."),
    TriageRule("r3", "Regra desligada.", active=False),
]
EXAMPLES = [
    FewShotExample("e1", "Agregamos truck baú", True, "eu chamei", NOW),
    FewShotExample("e2", "Vendo truck", False, "vendendo", NOW),
]
KEEP = RuleVerdict(
    keep=True,
    vehicle="truck",
    body="bau",
    driver_offering=True,
    region=RegionVerdict(RegionStatus.INSIDE, "guarulhos (0km)", "guarulhos", None),
)


def post(text: str = "Precisamos de agregado truck baú", image_url: str | None = None) -> Post:
    return Post(id="p1", text=text, url="https://fb.com/p1", posted_at=NOW, image_url=image_url)


def usage(**tokens):
    base = {
        "input_tokens": 100,
        "output_tokens": 30,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 900,
    }
    base.update(tokens)
    return SimpleNamespace(**base)


def reply(output: JudgeOutput | None, *, stop_reason: str = "end_turn"):
    return SimpleNamespace(parsed_output=output, stop_reason=stop_reason, usage=usage())


def bad_request() -> anthropic.BadRequestError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request)
    return anthropic.BadRequestError("imagem inválida", response=response, body=None)


class FakeMessages:
    """Imita `client.messages.parse`: registra cada chamada e devolve o roteiro."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.replies.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def judge_with(*replies) -> tuple[ClaudeJudge, FakeMessages]:
    messages = FakeMessages(*replies)
    client = SimpleNamespace(messages=messages)
    return ClaudeJudge(client=client, rules=RULES, examples=EXAMPLES), messages


GOOD = JudgeOutput(approved=True, score=0.9, reason="Empresa com carga em Guarulhos")


# --- prompt -----------------------------------------------------------------------


def test_system_prompt_has_active_rules_and_examples():
    judge, _ = judge_with()
    system = judge.system_prompt
    assert "1. Aprove empresa com carga para truck baú." in system
    assert "2. Reprove motorista se oferecendo." in system
    assert "Regra desligada" not in system
    assert "Agregamos truck baú" in system and "VALE O CONTATO" in system
    assert "Motivo dele: vendendo" in system


def test_request_uses_cache_structured_output_and_low_effort():
    judge, messages = judge_with(reply(GOOD))
    judge.judge(post(), KEEP)
    [call] = messages.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_format"] is JudgeOutput
    assert call["output_config"] == {"effort": "low"}


def test_user_message_carries_post_and_rule_signals():
    judge, messages = judge_with(reply(GOOD))
    judge.judge(post(), KEEP)
    [text_block] = messages.calls[0]["messages"][0]["content"]
    assert "Precisamos de agregado truck baú" in text_block["text"]
    assert "veículo citado: truck" in text_block["text"]
    assert "dentro do raio" in text_block["text"]
    assert "parece motorista se oferecendo" in text_block["text"]


def test_image_goes_before_the_text_by_url():
    judge, messages = judge_with(reply(GOOD))
    judge.judge(post(text="", image_url="https://fbcdn.net/flyer.jpg"), KEEP)
    image, text = messages.calls[0]["messages"][0]["content"]
    assert image == {
        "type": "image",
        "source": {"type": "url", "url": "https://fbcdn.net/flyer.jpg"},
    }
    assert "o conteúdo está na imagem" in text["text"]


# --- resposta -----------------------------------------------------------------------


def test_output_becomes_a_triage_result():
    judge, _ = judge_with(reply(GOOD))
    result = judge.judge(post(), KEEP)
    assert result.approved
    assert result.score == 0.9
    assert result.reason == "Empresa com carga em Guarulhos"
    assert result.vehicle == "truck"  # o juiz não disse; vem das regras
    assert result.origin == "guarulhos"


def test_score_is_clamped():
    judge, _ = judge_with(reply(JudgeOutput(approved=True, score=1.7, reason="ok")))
    assert judge.judge(post(), KEEP).score == 1.0


def test_usage_is_accumulated():
    judge, _ = judge_with(reply(GOOD), reply(GOOD))
    judge.judge(post(), KEEP)
    judge.judge(post(), KEEP)
    assert judge.usage.calls == 2
    assert judge.usage.cache_read_tokens == 1800


# --- falhas ---------------------------------------------------------------------------


def test_broken_image_is_retried_without_it():
    """URL de imagem do Facebook expira: o juiz tenta de novo só com o texto."""
    judge, messages = judge_with(bad_request(), reply(GOOD))
    result = judge.judge(post(image_url="https://fbcdn.net/expirada.jpg"), KEEP)
    assert result.approved
    assert len(messages.calls) == 2
    retry_blocks = messages.calls[1]["messages"][0]["content"]
    assert [block["type"] for block in retry_blocks] == ["text"]
    assert "não pôde ser carregada" in retry_blocks[0]["text"]


def test_refusal_becomes_a_rejection():
    judge, _ = judge_with(reply(None, stop_reason="refusal"))
    result = judge.judge(post(), KEEP)
    assert not result.approved
    assert result.reason == "juiz indisponível: recusou o post"


def test_api_error_does_not_stop_the_collection():
    judge, _ = judge_with(bad_request())
    result = judge.judge(post(), KEEP)
    assert not result.approved
    assert result.reason == "juiz indisponível: BadRequestError"


# --- custo ------------------------------------------------------------------------------


def test_cost_counts_cache_reads_at_a_tenth():
    totals = UsageTotals(calls=1, input_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert totals.cost_usd("claude-haiku-5-5") == pytest.approx(0.10 + 0.01)
    assert totals.cost_usd("modelo-desconhecido") is None
