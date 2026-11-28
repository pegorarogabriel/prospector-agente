"""O bot do Telegram: implementação da porta `Messenger` sobre a Bot API.

Ver `docs/arquitetura.md` seção 8. Sem servidor: as respostas são lidas com
`getUpdates` pela rotina de 2 em 2 horas.

Duas funções puras fazem o trabalho de verdade e são testadas sem rede:
`render_post` (o post vira texto e botões) e `parse_update` (o que o Telegram
devolve vira `Reply`). A classe só faz o HTTP.

**O token nunca aparece em erro.** Ele vai na URL (`/bot<token>/método`), e
uma exceção do httpx mostra a URL. Os logs do GitHub Actions de um repositório
público são públicos; por isso todo erro de rede é traduzido antes de subir.
"""

import html
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from prospector.core.feedback import FeedbackSource
from prospector.core.notification import (
    DONE_CHOICE,
    OTHER_CHOICE,
    RIGHT_CHOICE,
    Reply,
    reject_choice,
)
from prospector.flows.ports import PostMessage, ReplyBatch, SentMessage

API_URL = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_SECONDS = 30.0

#: Quanto do texto do post vai na mensagem. O link leva ao post inteiro.
SNIPPET_LENGTH = 600

#: Botão de celular com texto longo é cortado pelo próprio Telegram; melhor cortar antes.
BUTTON_LENGTH = 40


class TelegramError(RuntimeError):
    """Falha na Bot API, com a descrição do Telegram e sem o token."""


def _age(age: timedelta) -> str:
    minutes = max(int(age.total_seconds() // 60), 0)
    if minutes < 60:
        return f"há {minutes}min"
    hours = minutes // 60
    return f"há {hours}h" if hours < 48 else f"há {hours // 24} dias"


def _button(text: str, choice: str) -> dict[str, str]:
    label = text if len(text) <= BUTTON_LENGTH else text[: BUTTON_LENGTH - 1] + "…"
    return {"text": label, "callback_data": choice}


def render_post(message: PostMessage) -> dict[str, Any]:
    """O corpo do `sendMessage`, sem o `chat_id`.

    O link do post vai no texto, e não num botão: quando a resposta é gravada,
    os botões são trocados por um só ("Registrado: ..."), e o link continua lá.
    """
    post, result = message.post, message.result
    snippet = " ".join(post.text.split())
    if len(snippet) > SNIPPET_LENGTH:
        snippet = snippet[: SNIPPET_LENGTH - 1] + "…"
    quote = f"<i>“{html.escape(snippet)}”</i>" if snippet else "<i>(só imagem)</i>"

    place = result.origin or post.location or "local não identificado"
    if result.destination:
        place = f"{place} → {result.destination}"
    details = [html.escape(place), _age(message.age)]
    if message.queries:
        details.append("receita: " + html.escape(", ".join(message.queries)))

    audit = message.source is FeedbackSource.AUDIT
    header = (
        "<b>Auditoria: descartei este post. Confirma?</b>" if audit else "<b>Novo post aprovado</b>"
    )
    why = "Por que descartei" if audit else "Por que aprovei"
    text = "\n".join(
        [
            header,
            quote,
            "",
            " · ".join(details),
            f"{why}: {html.escape(result.reason)}",
            f'<a href="{html.escape(post.url, quote=True)}">Abrir post</a>',
        ]
    )

    # Nas duas perguntas, ✓ quer dizer "você decidiu bem" (core/feedback.py).
    keyboard = [[_button("✓ Descarte certo" if audit else "✓ Certo", RIGHT_CHOICE)]]
    keyboard += [
        [_button(f"✗ {reason}", reject_choice(index))]
        for index, reason in enumerate(message.reject_reasons)
    ]
    keyboard.append([_button("✗ Outro (responda com o motivo)", OTHER_CHOICE)])

    # Com imagem, a prévia do link mostra o flyer; sem imagem, a prévia do
    # Facebook é só a tela de login, então fica desligada.
    preview: dict[str, Any] = (
        {"url": post.image_url, "prefer_large_media": True}
        if post.image_url
        else {"is_disabled": True}
    )
    return {
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": keyboard},
        "link_preview_options": preview,
    }


def parse_update(update: Mapping[str, Any], *, chat_id: int, now: datetime) -> Reply | None:
    """Traduz uma atualização em `Reply`, ou `None` se não for resposta minha.

    Qualquer pessoa pode achar o bot e mandar mensagem. Só conta o que vem do
    meu chat (`TELEGRAM_CHAT_ID`), e só toque em botão ou reply a uma mensagem.
    O toque em botão não traz horário; vale o da leitura (`now`).
    """
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return None

    callback = update.get("callback_query")
    if isinstance(callback, Mapping):
        message = callback.get("message") or {}
        if (message.get("chat") or {}).get("id") != chat_id:
            return None
        data = callback.get("data")
        if not isinstance(data, str) or not isinstance(message.get("message_id"), int):
            return None
        return Reply(update_id, chat_id, message["message_id"], now, choice=data)

    message = update.get("message")
    if isinstance(message, Mapping):
        if (message.get("chat") or {}).get("id") != chat_id:
            return None
        original = message.get("reply_to_message") or {}
        text = message.get("text")
        if not isinstance(original.get("message_id"), int) or not isinstance(text, str):
            return None
        sent = message.get("date")
        when = datetime.fromtimestamp(sent, UTC) if isinstance(sent, int) else now
        return Reply(update_id, chat_id, original["message_id"], when, text=text)

    return None


def next_offset(updates: list[Mapping[str, Any]]) -> int | None:
    """O offset que confirma tudo que foi lido, inclusive o que foi ignorado."""
    ids = [item["update_id"] for item in updates if isinstance(item.get("update_id"), int)]
    return max(ids) + 1 if ids else None


class TelegramMessenger:
    """Implementa `Messenger` para um bot e um chat."""

    def __init__(self, token: str, chat_id: int, *, client: httpx.Client | None = None) -> None:
        self._token = token
        self.chat_id = chat_id
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)

    def _call(self, method: str, payload: Mapping[str, Any]) -> Any:  # noqa: ANN401 - JSON
        url = API_URL.format(token=self._token, method=method)
        try:
            response = self._client.post(url, json=dict(payload))
        except httpx.HTTPError as error:
            # Sem `from error`: a exceção original carrega a URL, com o token.
            raise TelegramError(f"{method}: falha de rede ({type(error).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code} sem JSON") from None
        if not body.get("ok"):
            description = body.get("description", "sem descrição")
            raise TelegramError(f"{method}: {description}")
        return body["result"]

    def send_post(self, message: PostMessage) -> SentMessage:
        result = self._call("sendMessage", {"chat_id": self.chat_id, **render_post(message)})
        return SentMessage(chat_id=result["chat"]["id"], message_id=result["message_id"])

    def send_text(self, text: str) -> SentMessage:
        result = self._call("sendMessage", {"chat_id": self.chat_id, "text": text})
        return SentMessage(chat_id=result["chat"]["id"], message_id=result["message_id"])

    def fetch_updates(self, offset: int | None) -> list[Mapping[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": 0,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return list(self._call("getUpdates", payload))

    def fetch_replies(self, offset: int | None) -> ReplyBatch:
        updates = self.fetch_updates(offset)
        now = datetime.now(UTC)
        replies = [
            reply
            for update in updates
            if (reply := parse_update(update, chat_id=self.chat_id, now=now)) is not None
        ]
        return ReplyBatch(replies=replies, next_offset=next_offset(updates))

    def confirm(self, chat_id: int, message_id: int, label: str) -> None:
        self._call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": [[_button(label, DONE_CHOICE)]]},
            },
        )


def chats_from_updates(updates: list[Mapping[str, Any]]) -> dict[int, str]:
    """Quem mandou mensagem para o bot: id do chat e nome. Serve para achar o meu."""
    chats: dict[int, str] = {}
    for update in updates:
        message = update.get("message") or (update.get("callback_query") or {}).get("message")
        chat = (message or {}).get("chat") or {}
        if isinstance(chat.get("id"), int):
            name = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
            chats[chat["id"]] = str(name)
    return chats
