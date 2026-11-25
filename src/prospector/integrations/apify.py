"""Busca no Apify: actor `scraper_one/facebook-posts-search`.

Ver `docs/arquitetura.md` seções 5 e 7. A tradução do item bruto para `Post`
foi portada de `app/processing/normalization.py` do FacebookProspector, sem os
campos de autor. Ela é uma função pura (`parse_item`), testada com itens
sintéticos: os testes nunca chamam a API real.
"""

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from prospector.core.post import Post
from prospector.core.text import collapse, normalize_text
from prospector.flows.ports import SearchResult

API_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

#: O actor roda de forma síncrona; a API do Apify corta em 300 s.
TIMEOUT_SECONDS = 330.0

_ID_FROM_URL = [
    re.compile(r"/posts/(pfbid[0-9A-Za-z]+|\d+)"),
    re.compile(r"/permalink/(\d+)"),
    re.compile(r"story_fbid=(\d+)"),
    re.compile(r"/videos/(\d+)"),
    re.compile(r"(pfbid[0-9A-Za-z]+)"),
]

# "May be an image of text that says 'AGREGAMOS TRUCK BAÚ'". O Facebook gera
# esse texto alternativo e às vezes transcreve o texto da imagem entre aspas.
# Só a transcrição entra no texto do post: a descrição genérica é inglês
# ("may be an image of", "that says") e confundiria a regra de idioma.
_CAPTION_QUOTE = re.compile(
    r"(?:says|diz|dice)\s*[:\s]*[\"'\u201c\u2018](?P<text>.+?)[\"'\u201d\u2019]\s*\.?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _first(data: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _attachments(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [att for att in item.get("attachments") or [] if isinstance(att, Mapping)]


def caption_text(caption: str | None) -> str | None:
    """O texto que o Facebook leu na imagem, sem a descrição genérica."""
    if not caption:
        return None
    match = _CAPTION_QUOTE.search(caption.strip())
    return collapse(match.group("text")) if match else None


def analysis_text(item: Mapping[str, Any]) -> str:
    """Texto do post + texto lido nas imagens, sem repetição.

    Descoberta do projeto antigo: post com `postText` vazio pode trazer a oferta
    inteira dentro da imagem. Analisar só o `postText` perde esses posts.
    """
    parts: list[str] = []
    body = _first(item, "postText", "text", "message")
    if isinstance(body, str) and body.strip():
        parts.append(body)
    for att in _attachments(item):
        transcribed = caption_text(att.get("accessibilityCaption"))
        if transcribed:
            parts.append(transcribed)

    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        key = normalize_text(part)
        if key and key not in seen:
            seen.add(key)
            unique.append(collapse(part))
    return "\n".join(unique)


def posted_at(item: Mapping[str, Any]) -> datetime | None:
    """Quando o post foi publicado. `timestamp` vem em milissegundos."""
    raw = _first(item, "timestamp", "time", "publishTime", "createdAt")
    if isinstance(raw, str) and re.fullmatch(r"\d{9,14}", raw.strip()):
        raw = float(raw.strip())
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        seconds = raw / 1000.0 if raw > 1e11 else float(raw)
        try:
            moment = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return moment if 2000 < moment.year < 2100 else None
    if isinstance(raw, str):
        try:
            moment = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    return None


def image_url(item: Mapping[str, Any]) -> str | None:
    """A primeira foto do post. Vídeo não conta: a URL dele não é uma imagem."""
    for att in _attachments(item):
        if att.get("type", "photo") != "photo":
            continue
        url = _first(att, "url", "image", "imageUrl", "src")
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


def post_id(item: Mapping[str, Any], text: str, when: datetime) -> str:
    """Id estável: `postId` do Facebook, senão o da URL, senão um hash do conteúdo.

    O hash usa texto + dia, sem o grupo: a mesma pessoa posta o mesmo frete em
    vários grupos, e é uma oportunidade só.
    """
    raw = _first(item, "postId", "post_id", "id")
    if raw and str(raw).strip().lower() not in ("none", "null"):
        return f"fb-{str(raw).strip()}"
    url = str(_first(item, "url", "postUrl", "link") or "")
    for pattern in _ID_FROM_URL:
        match = pattern.search(url)
        if match:
            return f"fb-{match.group(1)}"
    base = f"{normalize_text(text)[:800]}|{when:%Y-%m-%d}"
    return "fb-h" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def parse_item(item: Mapping[str, Any]) -> Post | None:
    """Item bruto do actor -> `Post`. `None` quando não há o que aproveitar.

    Sem data não dá para saber se o post é recente; sem texto e sem imagem não
    há o que julgar. Os dois casos são cobrados pelo Apify mesmo assim.

    `recipe_ids` sai vazio: quem sabe de qual receita veio a busca é o caso de
    uso, que anota isso no post.
    """
    if not isinstance(item, Mapping):
        return None
    when = posted_at(item)
    if when is None:
        return None
    text = analysis_text(item)
    image = image_url(item)
    if not text and not image:
        return None
    return Post(
        id=post_id(item, text, when),
        text=text,
        url=str(_first(item, "url", "postUrl", "link") or ""),
        posted_at=when,
        image_url=image,
    )


class ApifySearchProvider:
    """Implementa `SearchProvider` chamando o actor de forma síncrona."""

    def __init__(self, token: str, actor_id: str, *, client: httpx.Client | None = None) -> None:
        if not token:
            raise ValueError("APIFY_TOKEN não configurado")
        self._token = token
        self._actor = actor_id.replace("/", "~")
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)

    def search(
        self, query: str, params: Mapping[str, str], *, limit: int, since: datetime
    ) -> SearchResult:
        # startDate é um filtro grosseiro: o actor não expressa "últimas 48 h".
        # Um dia de folga para não perder post por fuso; a idade exata é regra.
        start = (since - timedelta(days=1)).strftime("%Y-%m-%d")
        payload = {"searchType": "latest", **params, "query": query, "resultsCount": limit}
        payload["startDate"] = start
        response = self._client.post(
            API_URL.format(actor=self._actor),
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        response.raise_for_status()
        items = response.json()
        if not isinstance(items, list):
            raise RuntimeError(f"resposta inesperada do Apify: {type(items).__name__}")
        posts = [post for item in items if (post := parse_item(item))]
        return SearchResult(posts=posts, charged=len(items))
