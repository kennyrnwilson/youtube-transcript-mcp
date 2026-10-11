import pytest

from youtube_transcript_mcp.errors import YouTubeError
from youtube_transcript_mcp.metadata import (
    MetadataClient,
    format_duration,
    parse_duration,
    read_api_key,
)

ITEM = {
    "id": "abcdefghijk",
    "snippet": {
        "title": "T",
        "channelTitle": "C",
        "channelId": "UC1",
        "publishedAt": "2020-01-01T00:00:00Z",
        "description": "D",
    },
    "contentDetails": {"duration": "PT1H2M3S"},
}
OEMBED = {"title": "T", "author_name": "C"}


@pytest.mark.parametrize(
    "iso,seconds",
    [
        ("PT1H2M3S", 3723),
        ("PT45S", 45),
        ("PT10M", 600),
        ("P1DT1S", 86401),
        ("P0D", 0),
        ("", None),
        ("garbage", None),
        ("PT", None),
    ],
)
def test_parse_duration(iso, seconds):
    assert parse_duration(iso) == seconds


def test_format_duration():
    assert format_duration(3723) == "1:02:03"
    assert format_duration(65) == "1:05"
    assert format_duration(None) is None


def test_data_api():
    client = MetadataClient("key", videos_list=lambda k, v: {"items": [ITEM]})
    m = client.fetch("abcdefghijk")
    assert m["source"] == "data-api"
    assert m["duration"] == "1:02:03"
    assert m["channel_id"] == "UC1"


def test_not_found_is_video_unavailable():
    client = MetadataClient("key", videos_list=lambda k, v: {"items": []})
    with pytest.raises(YouTubeError) as e:
        client.fetch("abcdefghijk")
    assert e.value.code == "video_unavailable"


def test_rejected_key_falls_back_to_oembed():
    def boom(k, v):
        raise RuntimeError("API key not valid")

    m = MetadataClient("bad", videos_list=boom, oembed=lambda v: OEMBED).fetch("abcdefghijk")
    assert m["source"] == "oembed"
    assert m["title"] == "T"
    assert "fell back" in m["note"]


def test_no_key_uses_oembed():
    m = MetadataClient(None, oembed=lambda v: OEMBED).fetch("abcdefghijk")
    assert m["source"] == "oembed"


def test_read_api_key_from_file(tmp_path, monkeypatch):
    f = tmp_path / "key"
    f.write_text("secret\n")
    monkeypatch.setenv("YOUTUBE_API_KEY_FILE", str(f))
    monkeypatch.setenv("YOUTUBE_API_KEY", "env")
    assert read_api_key() == "secret"

    monkeypatch.setenv("YOUTUBE_API_KEY_FILE", str(tmp_path / "missing"))
    assert read_api_key() is None

    monkeypatch.delenv("YOUTUBE_API_KEY_FILE")
    assert read_api_key() == "env"
