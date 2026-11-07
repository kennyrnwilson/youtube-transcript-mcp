"""FastMCP server: YouTube video metadata and transcripts.

The transcript tools are read-only and stateless. The frame tools download a
video once into ``YOUTUBE_MCP_DATA_DIR`` and cut frames and contact sheets from
it; they return the image inline, so the model sees it, and a signed link, so a
client with a shell can save it. Failures surface as tool errors whose message
starts with one of the codes in ``errors.py``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock, ImageContent, ResourceLink, TextContent

from youtube_transcript_mcp.errors import YouTubeError
from youtube_transcript_mcp.frames import FrameStore, label_time, parse_time, shrink
from youtube_transcript_mcp.metadata import MetadataClient, read_api_key
from youtube_transcript_mcp.transcript import TranscriptClient, page
from youtube_transcript_mcp.video_id import require_video_id

DEFAULT_MAX_CHARS = 60_000
MAX_FRAMES_PER_CALL = 12
SHEET_PREVIEW_WIDTH = 1600
FRAME_PREVIEW_WIDTH = 640


def sign(uri: str, exp: int, secret: str) -> str:
    """nginx secure_link: base64url(MD5(<exp><uri><secret>)), unpadded. Secret last."""
    digest = hashlib.md5(f"{exp}{uri}{secret}".encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _secret_from_env() -> str | None:
    path = os.environ.get("YOUTUBE_FRAMES_URL_SECRET_FILE")
    if path:
        return Path(path).read_text().strip()
    return os.environ.get("YOUTUBE_FRAMES_URL_SECRET")


def _transcript_page(
    transcripts: TranscriptClient,
    video_id: str,
    language: str,
    timestamps: bool,
    offset: int,
    max_chars: int,
) -> dict[str, Any]:
    transcript = transcripts.fetch(video_id, language)
    text = transcript.render(timestamps=timestamps)
    if offset > len(text):
        raise ValueError(f"offset {offset} is past the end ({len(text)} characters)")
    chunk, next_offset = page(text, offset, max_chars)
    return {
        "video_id": video_id,
        "language": transcript.track.language_code,
        "language_name": transcript.track.language,
        "is_generated": transcript.track.is_generated,
        "offset": offset,
        "total_chars": len(text),
        "next_offset": next_offset,
        "text": chunk,
    }


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 5106,
    transcripts: TranscriptClient | None = None,
    metadata: MetadataClient | None = None,
    frames: FrameStore | None = None,
    url_secret: str | None = None,
    public_base: str | None = None,
    url_path: str = "/mcp/youtube/frames/",
    url_ttl: int = 3600,
    clock=time.time,
) -> FastMCP:
    transcripts = transcripts or TranscriptClient()
    metadata = metadata or MetadataClient(read_api_key())
    if frames is None:
        data_dir = Path(os.environ.get("YOUTUBE_MCP_DATA_DIR", "/var/lib/youtube-mcp"))
        max_duration = int(os.environ.get("YOUTUBE_MCP_MAX_VIDEO_SECONDS", "3600"))
        frames = FrameStore(data_dir, max_duration=max_duration)
    url_secret = url_secret if url_secret is not None else _secret_from_env()
    public_base = public_base or os.environ.get(
        "YOUTUBE_MCP_PUBLIC_BASE", "https://gateway.kennyrnwilson.com"
    )
    video_locks: dict[str, asyncio.Lock] = {}

    mcp = FastMCP(
        "youtube-transcript",
        host=host,
        port=port,
        instructions=(
            "Fetch a YouTube video's metadata and transcript. Use get_video for the "
            "common case; page long transcripts with get_transcript(offset=next_offset). "
            "Errors start with a code: invalid_video_id, video_unavailable, no_transcript, "
            "or blocked (YouTube refused the server; not a property of the video). "
            "To illustrate a video, read the transcript with timestamps, look at "
            "get_contact_sheet for the stretch where a point is made, then take the "
            "clearest moments with get_frames. A frame's file name is a valid source "
            "for image-gen's edit_image."
        ),
    )

    async def _run(fn, *args):
        try:
            return await anyio.to_thread.run_sync(fn, *args)
        except YouTubeError as e:
            raise ToolError(str(e)) from e
        except ValueError as e:
            raise ToolError(f"invalid_request: {e}") from e

    def _link(path: Path) -> tuple[str | None, ResourceLink | None]:
        if not url_secret:
            return None, None
        uri = f"{url_path}{path.name}"
        exp = int(clock()) + url_ttl
        url = f"{public_base.rstrip('/')}{uri}?exp={exp}&sig={sign(uri, exp, url_secret)}"
        link = ResourceLink(
            type="resource_link",
            uri=url,
            name=path.name,
            mimeType="image/jpeg",
            size=path.stat().st_size,
        )
        return url, link

    def _image(data: bytes) -> ImageContent:
        return ImageContent(
            type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/jpeg"
        )

    async def _with_video_lock(video_id: str, fn):
        lock = video_locks.setdefault(video_id, asyncio.Lock())
        async with lock:
            return await _run(fn)

    @mcp.tool(structured_output=False)
    async def get_contact_sheet(
        url_or_id: str, start: str = "0:00", end: str | None = None, fps: float = 2.0
    ) -> list[ContentBlock]:
        """A grid of frames from part of a video, each labelled with its time, to
        choose which moments to illustrate. Shown inline.

        - start, end: "m:ss", "h:mm:ss" or seconds. end defaults to the end of the
          video. Speech usually leads the matching picture by a few seconds.
        - fps: frames per second of video, before thinning. A sheet holds at most
          64 frames, so a long range is spaced out evenly; narrow the range to see
          more detail.

        The first call for a video downloads it, which takes a few seconds."""
        video_id = require_video_id(url_or_id)
        start_s = parse_time(start)
        end_s = parse_time(end) if end else None
        sheet = await _with_video_lock(
            video_id, lambda: frames.sheet(video_id, start_s, end_s, fps)
        )
        data = sheet.path.read_bytes()
        url, link = _link(sheet.path)
        text = (
            f"Contact sheet: {len(sheet.times)} frames, {label_time(sheet.times[0])} to "
            f"{label_time(sheet.times[-1])}, labelled with their times. Take the ones you "
            f"want with get_frames(times=[...])."
        )
        if url:
            text += f"\nFull-size sheet (link expires in {url_ttl // 60} min): {url}"
        blocks: list[ContentBlock] = [TextContent(type="text", text=text)]
        blocks.append(_image(shrink(data, SHEET_PREVIEW_WIDTH)))
        if link:
            blocks.append(link)
        return blocks

    @mcp.tool(structured_output=False)
    async def get_frames(
        url_or_id: str, times: list[str], preview: bool = True
    ) -> list[ContentBlock]:
        """Full-resolution stills from a video at the given times, up to 12 a call.

        - times: each "m:ss", "m:ss.s", "h:mm:ss" or seconds, e.g. ["0:18.5", "33"].
        - preview: show each frame inline (scaled down) so you can check it.

        Each frame comes with a signed download link (curl -fsSL -o <file>.jpg
        '<url>') and its file name, frame-<32 hex>.jpg. Pass that name or link to
        image-gen's edit_image as `source` to annotate or change the frame."""
        video_id = require_video_id(url_or_id)
        if not times:
            raise ValueError("give at least one time")
        if len(times) > MAX_FRAMES_PER_CALL:
            raise ValueError(f"at most {MAX_FRAMES_PER_CALL} frames a call")
        seconds = [parse_time(t) for t in times]
        paths = await _with_video_lock(
            video_id, lambda: [frames.frame(video_id, s) for s in seconds]
        )
        blocks: list[ContentBlock] = []
        for s, path in zip(seconds, paths, strict=True):
            url, link = _link(path)
            text = f"Frame at {label_time(s)}: {path.name}"
            if url:
                text += f"\ncurl -fsSL -o <path>.jpg '{url}'"
            blocks.append(TextContent(type="text", text=text))
            if preview:
                blocks.append(_image(shrink(path.read_bytes(), FRAME_PREVIEW_WIDTH)))
            if link:
                blocks.append(link)
        if url_secret:
            blocks.append(
                TextContent(
                    type="text",
                    text=f"Links expire in {url_ttl // 60} minutes; download straight away.",
                )
            )
        return blocks

    @mcp.tool()
    async def get_video_metadata(url_or_id: str) -> dict[str, Any]:
        """Title, channel, channel ID, publish date, duration and description of a
        YouTube video. `source` says whether it came from the Data API or the
        keyless oEmbed fallback, which has only the title and channel."""
        return await _run(lambda: metadata.fetch(require_video_id(url_or_id)))

    @mcp.tool()
    async def list_transcript_languages(url_or_id: str) -> list[dict[str, Any]]:
        """Every transcript track a video has: language code, language name, and
        whether it is auto-generated."""

        def run():
            tracks = transcripts.list_tracks(require_video_id(url_or_id))
            return [
                {
                    "language": t.language_code,
                    "language_name": t.language,
                    "is_generated": t.is_generated,
                }
                for t in tracks
            ]

        return await _run(run)

    @mcp.tool()
    async def get_transcript(
        url_or_id: str,
        language: str = "en",
        timestamps: bool = False,
        offset: int = 0,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> dict[str, Any]:
        """A video's transcript, one page at a time. Falls back from `language` to
        manual English, generated English, then any track. `language` in the result
        is the one used. Read the next page with offset=next_offset; next_offset is
        null on the last page. timestamps=True puts each caption line on its own
        line with its start time."""
        return await _run(
            lambda: _transcript_page(
                transcripts, require_video_id(url_or_id), language, timestamps, offset, max_chars
            )
        )

    @mcp.tool()
    async def get_video(
        url_or_id: str, language: str = "en", max_chars: int = DEFAULT_MAX_CHARS
    ) -> dict[str, Any]:
        """Metadata plus the first transcript page, in one call. If the video has no
        transcript, or YouTube blocks the transcript request, `transcript` is null
        and `transcript_error` gives the reason. Continue a long transcript with
        get_transcript(offset=transcript.next_offset)."""

        def run():
            video_id = require_video_id(url_or_id)
            result = {
                "metadata": metadata.fetch(video_id),
                "transcript": None,
                "transcript_error": None,
            }
            try:
                result["transcript"] = _transcript_page(
                    transcripts, video_id, language, False, 0, max_chars
                )
            except YouTubeError as e:
                result["transcript_error"] = str(e)
            return result

        return await _run(run)

    return mcp
