"""Extract a YouTube video ID from a URL or a bare ID.

Copied from resource-librarian (``sources/video_ingestion.py``, commit 6674bdd)
and widened to accept ``/shorts/``, ``/live/``, ``m.youtube.com`` and
``music.youtube.com`` links.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from youtube_transcript_mcp.errors import YouTubeError

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}
_PATH_PREFIXES = {"embed", "shorts", "live", "v"}


def extract_video_id(url_or_id: str) -> str | None:
    """Return the 11-character video ID, or None if the input has none."""
    text = url_or_id.strip()
    if _ID.match(text):
        return text
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text

    parsed = urlparse(text)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    segments = [s for s in parsed.path.split("/") if s]

    candidate: str | None = None
    if host == "youtu.be" and segments:
        candidate = segments[0]
    elif host in _HOSTS:
        if parsed.path.rstrip("/") == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
        elif len(segments) >= 2 and segments[0] in _PATH_PREFIXES:
            candidate = segments[1]

    if candidate and _ID.match(candidate):
        return candidate
    return None


def require_video_id(url_or_id: str) -> str:
    video_id = extract_video_id(url_or_id)
    if video_id is None:
        raise YouTubeError("invalid_video_id", f"not a YouTube URL or video ID: {url_or_id!r}")
    return video_id
