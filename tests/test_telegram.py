"""A integração com o Telegram, sem rede: o HTTP passa por um transporte falso."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from prospector.core.notification import Reply
from prospector.core.post import Post, TriageResult
from prospector.flows.ports import PostMessage
from prospector.integrations.telegram import (
    TelegramError,
    TelegramMessenger,
    chats_from_updates,
    next_offset,
    parse_update,
    render_post,
)

NOW = datetime(2026, 11, 25, 10, tzinfo=UTC)
CHAT = 42
TOKEN = "123:segredo"


def message(text: str = "Agregamos <truck> & baú", image_url: str | None = None) -> PostMessage:
    post = Post(
        id="p1",
        text=text,
        url="https://fb.com/p1?a=1&b=2",
        posted_at=NOW,
        image_url=image_url,
    )
    return PostMessage(
        post=post,
        result=TriageResult(True, 0.9, "truck, carga seca", origin="Guarulhos", destination="SP"),
        queries=("agregado truck",),
        age=timedelta(hours=3),
        reject_reasons=("Fora da região", "Não é oportunidade"),
    )


# --- mensagem --------------------------------------------------------------


def test_render_escapes_html_and_shows_the_details():
    body = render_post(message())
    text = body["text"]
    assert "Agregamos &lt;truck&gt; &amp; baú" in text
    assert "Guarulhos → SP · há 3h · receita: agregado truck" in text
    assert "Por que aprovei: truck, carga seca" in text
    assert 'href="https://fb.com/p1?a=1&amp;b=2"' in text
    assert body["parse_mode"] == "HTML"
    assert body["link_preview_options"] == {"is_disabled": True}


def test_render_buttons_follow_the_offered_reasons():
    buttons = [row[0] for row in render_post(message())["reply_markup"]["inline_keyboard"]]
    assert [(b["text"], b["callback_data"]) for b in buttons] == [
        ("✓ Certo", "r"),
        ("✗ Fora da região", "w0"),
        ("✗ Não é oportunidade", "w1"),
        ("✗ Outro (responda com o motivo)", "o"),
    ]


def test_image_only_post_previews_the_image():
    body = render_post(message(text="", image_url="https://fbcdn.net/x.jpg"))
    assert "(só imagem)" in body["text"]
    assert body["link_preview_options"]["url"] == "https://fbcdn.net/x.jpg"


# --- atualizações ----------------------------------------------------------


def callback(update_id: int, chat: int = CHAT, data: str = "r") -> dict:
    bot_message = {"message_id": 100, "chat": {"id": chat}}
    return {
        "update_id": update_id,
        "callback_query": {"id": "q", "data": data, "message": bot_message},
    }


def text_reply(update_id: int, chat: int = CHAT, *, replying: bool = True) -> dict:
    msg = {"message_id": 200, "chat": {"id": chat}, "date": 1764064800, "text": "é mudança"}
    if replying:
        msg["reply_to_message"] = {"message_id": 100}
    return {"update_id": update_id, "message": msg}


def test_parse_button_tap():
    assert parse_update(callback(5), chat_id=CHAT, now=NOW) == Reply(5, CHAT, 100, NOW, choice="r")


def test_parse_text_reply_uses_the_message_date():
    reply = parse_update(text_reply(6), chat_id=CHAT, now=NOW)
    assert reply is not None
    assert (reply.message_id, reply.text) == (100, "é mudança")
    assert reply.answered_at == datetime.fromtimestamp(1764064800, UTC)


def test_strangers_and_loose_messages_are_ignored():
    assert parse_update(callback(5, chat=999), chat_id=CHAT, now=NOW) is None
    assert parse_update(text_reply(6, chat=999), chat_id=CHAT, now=NOW) is None
    assert parse_update(text_reply(7, replying=False), chat_id=CHAT, now=NOW) is None


def test_offset_confirms_even_the_ignored_updates():
    assert next_offset([callback(5), text_reply(9, chat=999)]) == 10
    assert next_offset([]) is None


def test_chats_from_updates():
    update = text_reply(1)
    update["message"]["chat"]["first_name"] = "Gabriel"
    assert chats_from_updates([update]) == {CHAT: "Gabriel"}


# --- HTTP ------------------------------------------------------------------


def messenger_with(handler) -> TelegramMessenger:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramMessenger(TOKEN, CHAT, client=client)


def test_send_post_calls_send_message_with_the_chat():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        result = {"message_id": 7, "chat": {"id": CHAT}}
        return httpx.Response(200, json={"ok": True, "result": result})

    sent = messenger_with(handler).send_post(message())
    assert (sent.chat_id, sent.message_id) == (CHAT, 7)
    path, payload = calls[0]
    assert path.endswith("/sendMessage")
    assert payload["chat_id"] == CHAT


def test_fetch_replies_filters_and_computes_the_offset():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["offset"] == 5
        return httpx.Response(200, json={"ok": True, "result": [callback(5), callback(6, chat=1)]})

    batch = messenger_with(handler).fetch_replies(5)
    assert [r.update_id for r in batch.replies] == [5]
    assert batch.next_offset == 7


def test_api_error_carries_the_description():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(TelegramError, match="chat not found"):
        messenger_with(handler).send_text("oi")


def test_network_error_never_shows_the_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"falhou em {request.url}")

    with pytest.raises(TelegramError) as caught:
        messenger_with(handler).send_text("oi")
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
