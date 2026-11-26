"""Cópia das imagens: o Storage (sem rede), a coleta e o backtest com a cópia."""

import random
from datetime import UTC, datetime

import httpx
import pytest

from prospector.cli import main
from prospector.core.feedback import Feedback, FeedbackSource, Verdict
from prospector.core.post import Post, TriageResult
from prospector.core.recipe import Recipe, RecipeStatus
from prospector.flows.backtest import run_backtest
from prospector.flows.collect import collect
from prospector.flows.ports import LabeledPost, SearchResult
from prospector.integrations.fakes import FakeImageStore, FakeJudge
from prospector.integrations.memory import InMemoryRepository
from prospector.integrations.supabase_storage import (
    MAX_IMAGE_BYTES,
    SupabaseImageStore,
    base_url,
    image_path,
)

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
PROJECT = "https://abc.supabase.co"
KEY = "sb_secret_xyz"


def post(post_id: str = "p1", text: str = "", image: str | None = "https://fbcdn.net/x.jpg"):
    return Post(
        id=post_id, text=text, url=f"https://fb.com/{post_id}", posted_at=NOW, image_url=image
    )


# --- Supabase Storage --------------------------------------------------------


def test_base_url_drops_the_rest_suffix_the_panel_shows():
    assert base_url(" https://abc.supabase.co/rest/v1/ ") == PROJECT
    assert base_url("https://abc.supabase.co") == PROJECT


def test_image_path_is_by_month_and_safe():
    assert image_path(post("pfbid0a/b?c"), "image/png") == "2026/11/pfbid0a_b_c.png"
    assert image_path(post(), "application/octet-stream").endswith(".jpg")


def store_with(handler) -> SupabaseImageStore:
    return SupabaseImageStore(
        PROJECT, KEY, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_save_downloads_and_uploads_with_the_key_in_the_header():
    uploads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fbcdn.net":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"JPG")
        uploads.append(request)
        return httpx.Response(200, json={"Key": "post-images/2026/11/p1.jpg"})

    assert store_with(handler).save(post()) == "2026/11/p1.jpg"
    [upload] = uploads
    assert upload.url.path == "/storage/v1/object/post-images/2026/11/p1.jpg"
    assert upload.headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in str(upload.url)
    assert upload.content == b"JPG"


@pytest.mark.parametrize(
    ("status", "content_type", "content", "error"),
    [
        (403, "text/html", b"", "download: HTTP 403"),
        (200, "text/html", b"<html>", "download: não é imagem (text/html)"),
        (200, "image/jpeg", b"x" * (MAX_IMAGE_BYTES + 1), "download: imagem grande demais"),
    ],
)
def test_bad_downloads_are_skipped_with_a_reason(status, content_type, content, error):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": content_type}, content=content)

    store = store_with(handler)
    assert store.save(post()) is None
    assert store.errors == [error]


def test_upload_refused_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fbcdn.net":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"JPG")
        return httpx.Response(400, json={"message": "new row violates row-level security policy"})

    store = store_with(handler)
    assert store.save(post()) is None
    assert "row-level security" in store.errors[0]


def test_link_is_signed_and_absolute():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/storage/v1/object/sign/post-images/2026/11/p1.jpg"
        return httpx.Response(
            200, json={"signedURL": "/object/sign/post-images/2026/11/p1.jpg?token=t"}
        )

    link = store_with(handler).link("2026/11/p1.jpg")
    assert link == f"{PROJECT}/storage/v1/object/sign/post-images/2026/11/p1.jpg?token=t"


def test_check_refuses_a_public_bucket():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "post-images", "public": True})

    assert "privado" in (store_with(handler).check() or "")


def test_check_reports_a_missing_bucket():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Bucket not found"})

    assert "Bucket not found" in (store_with(handler).check() or "")


def test_cli_refuses_the_publishable_key(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", PROJECT)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_publishable_abc")
    with pytest.raises(SystemExit, match="publicável"):
        main(["storage", "check"])


# --- coleta e backtest ---------------------------------------------------------


class OneShotSearch:
    def __init__(self, posts: list[Post]) -> None:
        self.posts = posts

    def search(self, query, params, *, limit, since) -> SearchResult:
        return SearchResult(posts=self.posts, charged=len(self.posts))


def test_collect_copies_images_only_of_judged_posts():
    repository = InMemoryRepository()
    repository.add_recipes([Recipe("r1", "agregado truck", status=RecipeStatus.ACTIVE)])
    flyer = post("flyer")
    junk = post("junk", text="Vendo caminhão truck baú, aceito troca")
    failing = post("failing")
    store = FakeImageStore()
    store.fail_for = {"failing"}

    report = collect(
        repository,
        OneShotSearch([flyer, junk, failing]),
        FakeJudge(),
        now=NOW,
        rng=random.Random(1),
        record_spend=False,
        write=True,
        image_store=store,
    )

    assert (report.images_saved, report.images_failed) == (1, 1)
    stored = {p.id: p.image_path for p in repository.list_posts()}
    assert stored == {"flyer": "fake/flyer.jpg", "junk": None, "failing": None}


def test_collect_without_write_copies_nothing():
    repository = InMemoryRepository()
    repository.add_recipes([Recipe("r1", "agregado truck", status=RecipeStatus.ACTIVE)])
    store = FakeImageStore()
    collect(
        repository,
        OneShotSearch([post()]),
        FakeJudge(),
        now=NOW,
        rng=random.Random(1),
        record_spend=False,
        write=False,
        image_store=store,
    )
    assert store.saved == {}


class SeeingJudge(FakeJudge):
    def __init__(self) -> None:
        self.images: list[str | None] = []

    def judge(self, post, verdict):
        self.images.append(post.image_url)
        return super().judge(post, verdict)


def labeled(post_id: str, image_path: str | None) -> LabeledPost:
    item = Post(
        id=post_id,
        text="",
        url="https://fb.com/x",
        posted_at=NOW,
        image_url="https://fbcdn.net/expirou.jpg",
        image_path=image_path,
    )
    answer = Feedback(post_id, Verdict.RIGHT, FeedbackSource.NOTIFICATION, NOW, "eu chamei")
    return LabeledPost(item, TriageResult(True, 0.9, "ok"), answer)


def test_backtest_judges_image_only_posts_from_the_stored_copy():
    judge = SeeingJudge()
    report = run_backtest(
        [labeled("com-copia", "2026/11/a.jpg"), labeled("sem-copia", None)],
        lambda examples: judge,
        image_link=FakeImageStore().link,
        examples_good=0,
        examples_bad=0,
    )
    assert report.skipped_image_only == 1
    assert report.stored_images == 1
    assert judge.images == ["https://storage.invalid/2026/11/a.jpg?token=fake"]


def test_backtest_without_storage_skips_image_only_posts():
    report = run_backtest(
        [labeled("com-copia", "2026/11/a.jpg")],
        lambda examples: FakeJudge(),
        examples_good=0,
        examples_bad=0,
    )
    assert (report.skipped_image_only, len(report.cases)) == (1, 0)
