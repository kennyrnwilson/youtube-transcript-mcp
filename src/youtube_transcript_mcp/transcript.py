"""Fetch transcripts with youtube-transcript-api (no API key needed).

Copied from resource-librarian (``sources/youtube_transcript.py``, commit
6674bdd), with two changes: failures are split into the codes in
``errors.py`` instead of one catch-all, and segment start times are kept so a
transcript can be rendered with timestamps.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from youtube_transcript_api import (
    InvalidVideoId,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

from youtube_transcript_mcp.errors import YouTubeError


@dataclass(frozen=True)
class Track:
    language_code: str
    language: str
    is_generated: bool


@dataclass(frozen=True)
class Segment:
    start: float
    text: str


@dataclass(frozen=True)
class Transcript:
    track: Track
    segments: tuple[Segment, ...]

    def render(self, timestamps: bool = False) -> str:
        if timestamps:
            return "\n".join(f"[{format_time(s.start)}] {s.text}" for s in self.segments)
        return " ".join(s.text for s in self.segments)


def format_time(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def page(text: str, offset: int, max_chars: int) -> tuple[str, int | None]:
    """Return ``text[offset:]`` cut to at most ``max_chars``, and the next offset.

    A page ends on whitespace, never inside a word, unless one word is longer
    than a page. The next offset is None on the last page.
    """
    if offset < 0 or max_chars < 1:
        raise ValueError("offset must be >= 0 and max_chars >= 1")
    end = offset + max_chars
    if end >= len(text):
        return text[offset:], None
    cut = max(text.rfind(" ", offset, end + 1), text.rfind("\n", offset, end + 1))
    if cut <= offset:
        return text[offset:end], end
    return text[offset:cut], cut + 1


def _track(t) -> Track:
    return Track(language_code=t.language_code, language=t.language, is_generated=t.is_generated)


def _segments(fetched) -> tuple[Segment, ...]:
    out = []
    for s in fetched:
        text = " ".join(s.text.split())
        if text:
            out.append(Segment(start=s.start, text=text))
    return tuple(out)


class TranscriptClient:
    """Lists and fetches transcripts. Keeps recent fetches in memory, so that
    reading a long transcript page by page asks YouTube once."""

    def __init__(
        self,
        api: YouTubeTranscriptApi | None = None,
        *,
        cache_size: int = 32,
        cache_ttl: float = 3600,
    ) -> None:
        self._api = api or YouTubeTranscriptApi()
        self._cache: OrderedDict[tuple[str, str], tuple[float, Transcript]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()

    def _list(self, video_id: str):
        try:
            return self._api.list(video_id)
        except RequestBlocked as e:
            raise YouTubeError("blocked", f"YouTube refused the request ({type(e).__name__})")
        except InvalidVideoId:
            raise YouTubeError("invalid_video_id", f"YouTube rejects {video_id!r} as a video ID")
        except VideoUnavailable:
            raise YouTubeError("video_unavailable", f"no public video has ID {video_id}")
        except TranscriptsDisabled:
            raise YouTubeError("no_transcript", f"video {video_id} has transcripts turned off")

    def list_tracks(self, video_id: str) -> list[Track]:
        return [_track(t) for t in self._list(video_id)]

    def fetch(self, video_id: str, language: str = "en") -> Transcript:
        key = (video_id, language)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self._cache_ttl:
                self._cache.move_to_end(key)
                return hit[1]

        transcript = self._fetch(video_id, language)
        with self._lock:
            self._cache[key] = (time.monotonic(), transcript)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return transcript

    def _fetch(self, video_id: str, language: str) -> Transcript:
        """Fallback order: the requested language, manual English, generated
        English, then the first track that fetches."""
        tracks = self._list(video_id)
        finders = [
            lambda: tracks.find_transcript([language]),
            lambda: tracks.find_manually_created_transcript(["en"]),
            lambda: tracks.find_generated_transcript(["en"]),
        ]
        candidates = []
        for find in finders:
            try:
                candidates.append(find())
            except NoTranscriptFound:
                pass
        candidates.extend(tracks)

        tried: set[tuple[str, bool]] = set()
        for t in candidates:
            if (t.language_code, t.is_generated) in tried:
                continue
            tried.add((t.language_code, t.is_generated))
            try:
                fetched = t.fetch()
            except RequestBlocked as e:
                raise YouTubeError("blocked", f"YouTube refused the request ({type(e).__name__})")
            except Exception:  # noqa: BLE001, S112 - one bad track should not stop the fallback
                continue
            segments = _segments(fetched)
            if segments:
                return Transcript(track=_track(t), segments=segments)

        raise YouTubeError("no_transcript", f"video {video_id} has no transcript in any language")
