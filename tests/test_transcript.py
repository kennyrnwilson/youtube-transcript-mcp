from types import SimpleNamespace

import pytest
from youtube_transcript_api import IpBlocked, NoTranscriptFound, TranscriptsDisabled

from youtube_transcript_mcp.errors import YouTubeError
from youtube_transcript_mcp.transcript import TranscriptClient, format_time, page

VID = "abcdefghijk"


class FakeTrack:
    def __init__(self, code, generated=False, segments=None, error=None):
        self.language_code = code
        self.language = code.upper()
        self.is_generated = generated
        self._segments = segments if segments is not None else [(0.0, f"hello {code}")]
        self._error = error
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        if self._error:
            raise self._error
        return [SimpleNamespace(start=s, text=t) for s, t in self._segments]


class FakeList(list):
    def _find(self, codes, generated):
        for code in codes:
            for t in self:
                if t.language_code == code and (generated is None or t.is_generated == generated):
                    return t
        raise NoTranscriptFound(VID, codes, self)

    def find_transcript(self, codes):
        try:
            return self._find(codes, False)
        except NoTranscriptFound:
            return self._find(codes, True)

    def find_manually_created_transcript(self, codes):
        return self._find(codes, False)

    def find_generated_transcript(self, codes):
        return self._find(codes, True)


class FakeApi:
    def __init__(self, tracks=None, error=None):
        self.tracks = FakeList(tracks or [])
        self.error = error
        self.lists = 0

    def list(self, video_id):
        self.lists += 1
        if self.error:
            raise self.error
        return self.tracks


def test_requested_language_first():
    api = FakeApi([FakeTrack("en"), FakeTrack("sv")])
    t = TranscriptClient(api).fetch(VID, "sv")
    assert t.track.language_code == "sv"


def test_falls_back_to_manual_then_generated_english():
    api = FakeApi([FakeTrack("en", generated=True), FakeTrack("de")])
    t = TranscriptClient(api).fetch(VID, "sv")
    assert (t.track.language_code, t.track.is_generated) == ("en", True)


def test_falls_back_to_first_track_that_fetches():
    broken = FakeTrack("fr", error=RuntimeError("bad xml"))
    api = FakeApi([broken, FakeTrack("de")])
    t = TranscriptClient(api).fetch(VID, "sv")
    assert t.track.language_code == "de"
    assert broken.fetches == 1


def test_no_tracks_is_no_transcript():
    with pytest.raises(YouTubeError) as e:
        TranscriptClient(FakeApi([])).fetch(VID)
    assert e.value.code == "no_transcript"


def test_disabled_is_no_transcript():
    with pytest.raises(YouTubeError) as e:
        TranscriptClient(FakeApi(error=TranscriptsDisabled(VID))).fetch(VID)
    assert e.value.code == "no_transcript"


def test_blocked_on_list_and_on_fetch():
    with pytest.raises(YouTubeError) as e:
        TranscriptClient(FakeApi(error=IpBlocked(VID))).fetch(VID)
    assert e.value.code == "blocked"

    api = FakeApi([FakeTrack("en", error=IpBlocked(VID)), FakeTrack("de")])
    with pytest.raises(YouTubeError) as e:
        TranscriptClient(api).fetch(VID)
    assert e.value.code == "blocked"


def test_cache_avoids_refetch():
    api = FakeApi([FakeTrack("en")])
    client = TranscriptClient(api)
    client.fetch(VID)
    client.fetch(VID)
    assert api.lists == 1


def test_render_with_timestamps():
    api = FakeApi([FakeTrack("en", segments=[(0, "one"), (75.5, " two\nlines "), (3725, "three")])])
    t = TranscriptClient(api).fetch(VID)
    assert t.render() == "one two lines three"
    assert t.render(timestamps=True) == "[0:00] one\n[1:15] two lines\n[1:02:05] three"
    assert format_time(59.9) == "0:59"


def test_page_breaks_on_whitespace():
    text = "alpha beta gamma delta"
    chunk, nxt = page(text, 0, 12)
    assert (chunk, nxt) == ("alpha beta", 11)
    chunk, nxt = page(text, nxt, 12)
    assert (chunk, nxt) == ("gamma delta", None)


def test_page_reassembles_and_handles_long_words():
    text = "x" * 25 + " tail"
    pages, off = [], 0
    while off is not None:
        chunk, off = page(text, off, 10)
        pages.append(chunk)
    assert pages[0] == "x" * 10
    assert "".join(pages) == text
