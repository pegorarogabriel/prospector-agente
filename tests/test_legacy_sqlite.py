"""Leitura do `prospector.db` antigo."""

from datetime import datetime

import pytest

from prospector.integrations.legacy_sqlite import LegacySqliteSource


def test_reads_posts_with_all_their_queries(legacy_db):
    posts = {post.legacy_id: post for post in LegacySqliteSource(legacy_db).read_posts()}
    assert posts[1].queries == {"agregado truck", "agrega toco"}
    assert posts[2].queries == {"agrega toco"}


def test_parses_naive_dates_and_answers(legacy_db):
    [first, *_] = LegacySqliteSource(legacy_db).read_posts()
    assert first.posted_at == datetime(2026, 9, 7, 19, 8, 43)
    assert first.user_action == "CHAMEI"
    assert first.user_action_reason == "rota boa"


def test_missing_values_stay_none(legacy_db):
    third = LegacySqliteSource(legacy_db).read_posts()[2]
    assert third.text is None
    assert third.posted_at is None
    assert third.status == "NO_DATE"


def test_author_fields_are_not_read(legacy_db):
    for post in LegacySqliteSource(legacy_db).read_posts():
        assert "Fulano" not in repr(post)
        assert "fulano" not in repr(post)


def test_leaves_the_file_untouched(legacy_db):
    """A importação nunca pode alterar o histórico de onde veio."""
    before = legacy_db.read_bytes()
    LegacySqliteSource(legacy_db).read_posts()
    assert legacy_db.read_bytes() == before


def test_missing_file_fails_early(tmp_path):
    with pytest.raises(FileNotFoundError):
        LegacySqliteSource(tmp_path / "nao-existe.db")


def test_reads_image_url(legacy_db):
    second = LegacySqliteSource(legacy_db).read_posts()[1]
    assert second.text is None
    assert second.image_url == "https://fbcdn.net/2.jpg"
