import io
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from youtube_transcript_mcp.errors import YouTubeError
from youtube_transcript_mcp.frames import (
    FrameStore,
    _classify,
    frame_name,
    grid_shape,
    label_time,
    parse_time,
    sheet_name,
    sheet_times,
)
from youtube_transcript_mcp.metadata import MetadataClient
from youtube_transcript_mcp.server import build_server, sign
from youtube_transcript_mcp.transcript import TranscriptClient

VID = "Se1j7U8EI2o"
NOW = 1_790_000_000


def jpeg(width=1280, height=720, colour=(0, 120, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="JPEG")
    return out.getvalue()


class FakeDownloader:
    def __init__(self, duration=57):
        self.duration = duration
        self.downloads = 0

    def info(self, video_id):
        return {"duration": self.duration}

    def download(self, video_id, dest_dir):
        self.downloads += 1
        path = dest_dir / f"{video_id}.mp4"
        path.write_bytes(b"video")
        return path


class FakeGrab:
    def __init__(self, duration=57):
        self.duration = duration
        self.calls = []

    def __call__(self, video, seconds, *, width=None):
        self.calls.append((seconds, width))
        if seconds > self.duration:
            raise ValueError("past the end")
        return jpeg(width or 1280, round((width or 1280) * 9 / 16))


@pytest.fixture
def store(tmp_path):
    return FrameStore(tmp_path, FakeDownloader(), grab=FakeGrab())


# --- pure helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    "text,seconds", [("83.5", 83.5), ("1:23.5", 83.5), ("0:13", 13), ("1:01:23", 3683), (7, 7.0)]
)
def test_parse_time(text, seconds):
    assert parse_time(text) == seconds


@pytest.mark.parametrize("text", ["", "a", "1::2", "1:2:3:4", "-5", "nan"])
def test_parse_time_rejects_nonsense(text):
    with pytest.raises(ValueError):
        parse_time(text)


@pytest.mark.parametrize(
    "seconds,text", [(13, "0:13"), (18.5, "0:18.5"), (59.96, "1:00"), (3683, "1:01:23")]
)
def test_label_time(seconds, text):
    assert label_time(seconds) == text


def test_sheet_times_include_the_end_and_thin_to_the_cap():
    assert sheet_times(13, 15, 2) == [13, 13.5, 14, 14.5, 15]
    long = sheet_times(0, 600, 2)
    assert len(long) == 64 and long[-1] == 600


@pytest.mark.parametrize("start,end,fps", [(5, 5, 2), (5, 4, 2), (0, 10, 0)])
def test_sheet_times_reject_an_empty_range(start, end, fps):
    with pytest.raises(ValueError):
        sheet_times(start, end, fps)


@pytest.mark.parametrize("count,shape", [(1, (1, 1)), (8, (8, 1)), (9, (8, 2)), (64, (8, 8))])
def test_grid_shape(count, shape):
    assert grid_shape(count) == shape


def test_names_match_the_pattern_image_gen_accepts():
    import re

    assert re.fullmatch(r"frame-[0-9a-f]{32}\.jpg", frame_name(VID, 18.5))
    assert frame_name(VID, 18.5) == frame_name(VID, 18.5)
    assert frame_name(VID, 18.5) != frame_name(VID, 18.6)
    assert frame_name(VID, 18.5) != frame_name("abcdefghijk", 18.5)
    assert sheet_name(VID, [1, 2]).startswith("sheet-")


@pytest.mark.parametrize(
    "message,code",
    [
        ("Sign in to confirm you're not a bot", "blocked"),
        ("HTTP Error 429: Too Many Requests", "blocked"),
        ("Video unavailable", "video_unavailable"),
        ("Private video. Sign in", "video_unavailable"),
    ],
)
def test_download_errors_are_classified(message, code):
    assert _classify(Exception(message)).code == code


# --- the store ---------------------------------------------------------------


def test_a_video_is_downloaded_once(store):
    store.frame(VID, 10)
    store.frame(VID, 20)
    store.sheet(VID, 0, 5, 1)
    assert store.downloader.downloads == 1


def test_a_repeat_frame_is_a_cache_hit(store):
    first = store.frame(VID, 10)
    calls = len(store.grab.calls)
    assert store.frame(VID, 10) == first
    assert len(store.grab.calls) == calls


def test_frames_are_group_readable(store):
    assert store.frame(VID, 10).stat().st_mode & 0o777 == 0o640


def test_a_frame_past_the_end_is_refused(store):
    with pytest.raises(ValueError, match="past the end"):
        store.frame(VID, 58)


def test_a_video_over_the_limit_is_not_downloaded(tmp_path):
    store = FrameStore(tmp_path, FakeDownloader(duration=7200), grab=FakeGrab(), max_duration=3600)
    with pytest.raises(ValueError, match="limit"):
        store.frame(VID, 1)
    assert store.downloader.downloads == 0


def test_a_sheet_defaults_to_the_end_of_the_video(store):
    sheet = store.sheet(VID, 50, None, 1)
    assert sheet.times == [50, 51, 52, 53, 54, 55, 56, 57]
    image = Image.open(sheet.path)
    assert image.size == (8 * 320, 180)


def test_a_sheet_clamps_an_end_past_the_video(store):
    assert store.sheet(VID, 55, 120, 1).times[-1] == 57


def test_download_errors_pass_through(tmp_path):
    class Blocked(FakeDownloader):
        def info(self, video_id):
            raise YouTubeError("blocked", "no")

    store = FrameStore(tmp_path, Blocked(), grab=FakeGrab())
    with pytest.raises(YouTubeError):
        store.frame(VID, 1)


# --- the tools ---------------------------------------------------------------


def frames_server(store, secret="s3cret"):
    return build_server(
        transcripts=TranscriptClient(object()),
        metadata=MetadataClient(None, oembed=lambda v: {}),
        frames=store,
        url_secret=secret,
        public_base="https://gw.example",
        clock=lambda: NOW,
    )


async def blocks(mcp, tool, **args):
    result = await mcp.call_tool(tool, args)
    return result[0] if isinstance(result, tuple) else result


async def test_frames_tools_are_listed(store):
    names = {t.name for t in await frames_server(store).list_tools()}
    assert {"get_contact_sheet", "get_frames"} <= names


async def test_get_frames_returns_preview_link_and_name(store):
    out = await blocks(frames_server(store), "get_frames", url_or_id=VID, times=["0:18.5", "33"])
    texts = [b for b in out if b.type == "text"]
    links = [b for b in out if b.type == "resource_link"]
    images = [b for b in out if b.type == "image"]
    assert len(links) == 2 and len(images) == 2
    assert frame_name(VID, 18.5) in texts[0].text
    url = urlparse(str(links[0].uri))
    assert url.path == f"/mcp/youtube/frames/{frame_name(VID, 18.5)}"
    q = parse_qs(url.query)
    assert q["sig"][0] == sign(url.path, int(q["exp"][0]), "s3cret")
    assert int(q["exp"][0]) == NOW + 3600


async def test_get_frames_without_preview_sends_no_images(store):
    out = await blocks(
        frames_server(store), "get_frames", url_or_id=VID, times=["5"], preview=False
    )
    assert not [b for b in out if b.type == "image"]


async def test_without_a_secret_there_are_no_links(store):
    out = await blocks(frames_server(store, secret=""), "get_frames", url_or_id=VID, times=["5"])
    assert not [b for b in out if b.type == "resource_link"]
    assert [b for b in out if b.type == "image"]


async def test_get_frames_limits_the_count(store):
    with pytest.raises(Exception, match="at most 12"):
        await blocks(frames_server(store), "get_frames", url_or_id=VID, times=["1"] * 13)


async def test_get_contact_sheet_is_shown_inline(store):
    out = await blocks(
        frames_server(store), "get_contact_sheet", url_or_id=VID, start="0:13", end="0:15"
    )
    assert "5 frames, 0:13 to 0:15" in out[0].text
    assert out[1].type == "image"
    assert out[2].type == "resource_link"


async def test_bad_times_are_refused(store):
    with pytest.raises(Exception, match="not a time"):
        await blocks(frames_server(store), "get_frames", url_or_id=VID, times=["soon"])
