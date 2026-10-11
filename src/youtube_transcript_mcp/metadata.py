"""Fetch video metadata from the YouTube Data API v3, or from oEmbed.

Copied from resource-librarian (``sources/youtube_api.py``, commit 6674bdd).
Changes: the key comes from the file named in ``YOUTUBE_API_KEY_FILE``; the
ISO 8601 duration is parsed; and when there is no key, or the Data API refuses
it, the keyless oEmbed endpoint gives the title and channel instead.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from youtube_transcript_mcp.errors import YouTubeError

OEMBED_URL = "https://www.youtube.com/oembed"
_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def read_api_key() -> str | None:
    """The key from ``YOUTUBE_API_KEY_FILE``, else ``YOUTUBE_API_KEY`` (local runs)."""
    path = os.environ.get("YOUTUBE_API_KEY_FILE")
    if path:
        p = Path(path)
        key = p.read_text().strip() if p.is_file() else ""
        return key or None
    return os.environ.get("YOUTUBE_API_KEY", "").strip() or None


def parse_duration(value: str) -> int | None:
    """``PT1H2M3S`` -> 3723. None for anything unparseable (e.g. ``P0D`` live)."""
    m = _DURATION.match(value or "")
    if not m or not any(m.groupdict().values()):
        return None
    d = {k: int(v or 0) for k, v in m.groupdict().items()}
    return d["days"] * 86400 + d["hours"] * 3600 + d["minutes"] * 60 + d["seconds"]


def format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _data_api_videos(api_key: str, video_id: str) -> dict:
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    return youtube.videos().list(part="snippet,contentDetails", id=video_id).execute()


def _oembed(video_id: str) -> dict:
    query = urllib.parse.urlencode({"url": watch_url(video_id), "format": "json"})
    try:
        with urllib.request.urlopen(f"{OEMBED_URL}?{query}", timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            raise YouTubeError("video_unavailable", f"no public video has ID {video_id}")
        if e.code in (403, 429):
            raise YouTubeError("blocked", f"YouTube oEmbed refused the request (HTTP {e.code})")
        raise


class MetadataClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        videos_list: Callable[[str, str], dict] = _data_api_videos,
        oembed: Callable[[str], dict] = _oembed,
    ) -> None:
        self.api_key = api_key
        self._videos_list = videos_list
        self._oembed = oembed

    def fetch(self, video_id: str) -> dict[str, Any]:
        note = None
        if self.api_key:
            try:
                response = self._videos_list(self.api_key, video_id)
            except Exception as e:  # noqa: BLE001 - any Data API fault falls back
                note = f"Data API failed ({type(e).__name__}); fell back to oEmbed"
            else:
                items = response.get("items") or []
                if not items:
                    raise YouTubeError("video_unavailable", f"no public video has ID {video_id}")
                return self._from_data_api(items[0])
        else:
            note = "no YouTube Data API key configured; fell back to oEmbed"
        return self._from_oembed(video_id, note)

    @staticmethod
    def _from_data_api(item: dict) -> dict[str, Any]:
        snippet = item["snippet"]
        seconds = parse_duration(item.get("contentDetails", {}).get("duration", ""))
        return {
            "video_id": item["id"],
            "url": watch_url(item["id"]),
            "title": snippet["title"],
            "channel": snippet["channelTitle"],
            "channel_id": snippet["channelId"],
            "published_at": snippet["publishedAt"],
            "duration": format_duration(seconds),
            "duration_seconds": seconds,
            "description": snippet.get("description", ""),
            "source": "data-api",
        }

    def _from_oembed(self, video_id: str, note: str | None) -> dict[str, Any]:
        data = self._oembed(video_id)
        return {
            "video_id": video_id,
            "url": watch_url(video_id),
            "title": data.get("title"),
            "channel": data.get("author_name"),
            "channel_id": None,
            "published_at": None,
            "duration": None,
            "duration_seconds": None,
            "description": None,
            "source": "oembed",
            "note": note,
        }
