"""Implementações falsas das portas `SearchProvider`, `Judge`, `Messenger` e do LLM.

Servem aos testes e ao `prospector collect --dry-run`: o caminho inteiro da
coleta roda sem gastar crédito do Apify, sem chamar LLM e sem mandar nada pro
celular.

Os posts de exemplo (`samples/posts.json`) são inventados, no formato real do
actor, e cobrem cada caminho das regras: oferta boa, só imagem, venda,
espanhol, veículo incompatível, fora de SP, post velho, sem data.
"""

import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from importlib import resources
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from prospector.core.notification import Reply
from prospector.core.post import Post, TriageResult
from prospector.core.rules import RuleVerdict
from prospector.flows.ports import PostMessage, ReplyBatch, SearchResult, SentMessage
from prospector.integrations.apify import parse_item


def _load_samples() -> list[dict[str, Any]]:
    path = resources.files("prospector.integrations") / "samples" / "posts.json"
    return json.loads(path.read_text(encoding="utf-8"))


class SampleSearchProvider:
    """Devolve posts de exemplo como se viessem do Apify.

    As datas são relativas a `now` (cada exemplo diz há quantas horas foi
    postado), para os exemplos não envelhecerem e caírem na regra de idade.
    Cada busca sorteia uma amostra estável por termo: buscas diferentes se
    sobrepõem em parte, o que exercita a junção de duplicados.
    """

    def __init__(self, *, now: datetime, items: list[dict[str, Any]] | None = None) -> None:
        self._now = now
        self._items = items if items is not None else _load_samples()

    def _as_apify_item(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        item = {key: value for key, value in sample.items() if not key.startswith("_")}
        hours = item.pop("hours_ago", None)
        if hours is not None:
            moment = self._now - timedelta(hours=float(hours))
            item["timestamp"] = int(moment.timestamp() * 1000)
        return item

    def search(
        self, query: str, params: Mapping[str, str], *, limit: int, since: datetime
    ) -> SearchResult:
        pool = [self._as_apify_item(sample) for sample in self._items]
        rng = random.Random(query)
        chosen = rng.sample(pool, k=min(limit, len(pool)))
        posts = [post for item in chosen if (post := parse_item(item))]
        return SearchResult(posts=posts, charged=len(chosen))


class FakeJudge:
    """Aprova tudo que passou nas regras, com uma nota estável por post.

    A nota varia entre 0.50 e 0.90 (hash do id) só para o dry-run mostrar a
    ordenação dos aprovados e o corte das notificações. Não é uma opinião.
    """

    def judge(self, post: Post, verdict: RuleVerdict) -> TriageResult:
        digest = int(hashlib.sha256(post.id.encode()).hexdigest()[:8], 16)
        score = round(0.5 + 0.4 * (digest % 1000) / 1000, 2)
        reason = "juiz falso: passou nas regras"
        if verdict.driver_offering:
            reason += " (parece motorista se oferecendo)"
        return TriageResult(
            approved=True,
            score=score,
            reason=reason,
            vehicle=verdict.vehicle,
            origin=verdict.region.origin,
            destination=verdict.region.destination,
        )


class FakeMessenger:
    """Guarda o que seria enviado e devolve respostas pré-definidas.

    Imita o `getUpdates`: `fetch_replies(offset)` devolve só as respostas com
    `update_id >= offset`, como o Telegram faz depois de confirmar as antigas.
    """

    def __init__(self, *, chat_id: int = 1, first_message_id: int = 100) -> None:
        self.chat_id = chat_id
        self.sent: list[tuple[SentMessage, PostMessage]] = []
        self.replies: list[Reply] = []
        self.confirmed: list[tuple[int, int, str]] = []
        self.texts: list[tuple[SentMessage, str]] = []
        self.undo_offered: list[int] = []
        """Mensagens de resumo que levaram o botão Desfazer."""
        self.fail_for: set[str] = set()
        """Ids de post cujo envio deve falhar (para testar o erro isolado)."""
        self._next_id = first_message_id

    def send_post(self, message: PostMessage) -> SentMessage:
        if message.post.id in self.fail_for:
            raise RuntimeError("envio falso falhou")
        sent = SentMessage(self.chat_id, self._next_id)
        self._next_id += 1
        self.sent.append((sent, message))
        return sent

    def send_summary(self, text: str, *, undo: bool) -> SentMessage:
        sent = SentMessage(self.chat_id, self._next_id)
        self._next_id += 1
        self.texts.append((sent, text))
        if undo:
            self.undo_offered.append(sent.message_id)
        return sent

    def fetch_replies(self, offset: int | None) -> ReplyBatch:
        fresh = [r for r in self.replies if offset is None or r.update_id >= offset]
        last = max((r.update_id for r in fresh), default=None)
        return ReplyBatch(replies=fresh, next_offset=None if last is None else last + 1)

    def confirm(self, chat_id: int, message_id: int, label: str) -> None:
        self.confirmed.append((chat_id, message_id, label))


class FakeImageStore:
    """Finge guardar a imagem: devolve um caminho estável e lembra o que recebeu."""

    def __init__(self) -> None:
        self.saved: dict[str, str] = {}
        self.fail_for: set[str] = set()

    def save(self, post: Post) -> str | None:
        if not post.image_url or post.id in self.fail_for:
            return None
        path = f"fake/{post.id}.jpg"
        self.saved[path] = post.image_url
        return path

    def link(self, path: str) -> str | None:
        return f"https://storage.invalid/{path}?token=fake"


class ScriptedChatModel(BaseChatModel):
    """Um modelo de chat do LangChain que devolve respostas roteirizadas.

    É o fake do LLM do agente semanal. Cada chamada consome a próxima resposta
    do roteiro (uma `AIMessage`, com ou sem chamada de ferramenta) e guarda o
    que recebeu, para o teste conferir o prompt. Ferramentas são aceitas e
    ignoradas: quem decide o que "chamar" é o roteiro.
    """

    script: list[AIMessage] = Field(default_factory=list)
    received: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,  # noqa: ANN401 - assinatura do LangChain
    ) -> ChatResult:
        self.received.append(list(messages))
        reply = self.script.pop(0) if self.script else AIMessage(content="(fim do roteiro)")
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":  # noqa: ANN401
        return self
