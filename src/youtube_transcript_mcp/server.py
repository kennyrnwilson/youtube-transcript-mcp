"""FastMCP server: YouTube video metadata and transcripts.

Read-only and stateless. Failures surface as tool errors whose message starts
with one of the codes in ``errors.py``.
"""

from __future__ import annotations

from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from youtube_transcript_mcp.errors import YouTubeError
from youtube_transcript_mcp.metadata import MetadataClient, read_api_key
from youtube_transcript_mcp.transcript import TranscriptClient, page
from youtube_transcript_mcp.video_id import require_video_id

DEFAULT_MAX_CHARS = 60_000


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
) -> FastMCP:
    transcripts = transcripts or TranscriptClient()
    metadata = metadata or MetadataClient(read_api_key())

    mcp = FastMCP(
        "youtube-transcript",
        host=host,
        port=port,
        instructions=(
            "Fetch a YouTube video's metadata and transcript. Use get_video for the "
            "common case; page long transcripts with get_transcript(offset=next_offset). "
            "Errors start with a code: invalid_video_id, video_unavailable, no_transcript, "
            "or blocked (YouTube refused the server; not a property of the video)."
        ),
    )

    async def _run(fn, *args):
        try:
            return await anyio.to_thread.run_sync(fn, *args)
        except YouTubeError as e:
            raise ToolError(str(e)) from e

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
