"""Tradução do item bruto do Apify para `Post`. Nenhum teste chama a API real."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from prospector.integrations.apify import (
    ApifySearchProvider,
    analysis_text,
    caption_text,
    parse_item,
)

WHEN = datetime(2026, 11, 24, 9, 30, tzinfo=UTC)
TIMESTAMP_MS = int(WHEN.timestamp() * 1000)


def item(**fields):
    base = {
        "postId": "123",
        "url": "https://www.facebook.com/groups/g/posts/123",
        "postText": "Agregamos truck baú",
        "timestamp": TIMESTAMP_MS,
        "author": {"id": "9", "name": "Fulano de Tal", "profileUrl": "https://fb.com/fulano"},
        "attachments": [],
    }
    base.update(fields)
    return base


def photo(caption: str = "", url: str = "https://scontent.fbcdn.net/a.jpg") -> dict:
    return {"type": "photo", "id": "a", "url": url, "accessibilityCaption": caption}


def test_basic_item():
    post = parse_item(item())
    assert post.id == "fb-123"
    assert post.text == "Agregamos truck baú"
    assert post.posted_at == WHEN
    assert post.image_url is None
    assert post.recipe_ids == frozenset()


def test_author_fields_never_leave_the_integration():
    assert "Fulano" not in repr(parse_item(item()))


def test_timestamp_in_seconds_and_iso():
    assert parse_item(item(timestamp=int(WHEN.timestamp()))).posted_at == WHEN
    assert parse_item(item(timestamp="2026-11-24T09:30:00.000Z")).posted_at == WHEN


def test_first_photo_is_the_image():
    post = parse_item(
        item(attachments=[photo(url="https://x/1.jpg"), photo(url="https://x/2.jpg")])
    )
    assert post.image_url == "https://x/1.jpg"


def test_video_is_not_an_image():
    video = {"type": "video", "url": "https://video.fbcdn.net/v.mp4"}
    assert parse_item(item(attachments=[video])).image_url is None


def test_caption_transcription_joins_the_text():
    caption = "May be an image of truck and text that says 'AGREGAMOS TOCO BAÚ'"
    post = parse_item(item(postText="", attachments=[photo(caption)]))
    assert post.text == "AGREGAMOS TOCO BAÚ"
    assert post.image_url


def test_generic_caption_is_left_out():
    """A descrição em inglês confundiria a regra de idioma."""
    assert caption_text("May be an image of 2 people, truck and road") is None


def test_repeated_text_appears_once():
    caption = "May be an image of text that says 'Agregamos truck baú'"
    assert analysis_text(item(attachments=[photo(caption)])) == "Agregamos truck baú"


def test_portuguese_caption():
    assert caption_text("Pode ser uma imagem de texto que diz “PRECISO DE TOCO”") == (
        "PRECISO DE TOCO"
    )


def test_image_only_post_without_caption_is_kept():
    post = parse_item(item(postText="", attachments=[photo()]))
    assert post.text == ""
    assert post.image_url


def test_nothing_to_judge_is_none():
    assert parse_item(item(postText="", attachments=[])) is None


def test_no_date_is_none():
    assert parse_item(item(timestamp=None)) is None


def test_id_from_url_when_post_id_is_missing():
    post = parse_item(item(postId=None, url="https://www.facebook.com/groups/g/permalink/987/"))
    assert post.id == "fb-987"


def test_id_from_content_is_stable_across_groups():
    """O mesmo frete postado em dois grupos é uma oportunidade só."""
    a = parse_item(item(postId=None, url="https://fb.com/groups/a"))
    b = parse_item(item(postId=None, url="https://fb.com/groups/b"))
    assert a.id == b.id
    assert a.id.startswith("fb-h")


def test_provider_sends_query_and_counts_every_item_as_charged():
    """Item que não vira post também é cobrado pelo Apify."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json=[item(), item(postId="456", timestamp=None)])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ApifySearchProvider("tok", "scraper_one/facebook-posts-search", client=client)
    result = provider.search("agregado truck", {"searchType": "latest"}, limit=5, since=WHEN)

    assert "scraper_one~facebook-posts-search" in seen["url"]
    assert "tok" not in seen["url"]
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["query"] == "agregado truck"
    assert seen["body"]["resultsCount"] == 5
    assert seen["body"]["startDate"] == "2026-11-23"
    assert result.charged == 2
    assert [post.id for post in result.posts] == ["fb-123"]


def test_provider_requires_a_token():
    with pytest.raises(ValueError):
        ApifySearchProvider("", "actor")
