import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tests.test_transcript import FakeApi, FakeTrack
from youtube_transcript_mcp.metadata import MetadataClient
from youtube_transcript_mcp.server import build_server
from youtube_transcript_mcp.transcript import TranscriptClient

URL = "https://youtu.be/abcdefghijk"
WORDS = [(float(i), f"word{i}") for i in range(100)]


def server(tracks=None):
    return build_server(
        transcripts=TranscriptClient(
            FakeApi(tracks if tracks is not None else [FakeTrack("en", segments=WORDS)])
        ),
        metadata=MetadataClient(None, oembed=lambda v: {"title": "T", "author_name": "C"}),
    )


async def call(mcp, name, **args):
    _, structured = await mcp.call_tool(name, args)
    return structured.get("result", structured)


async def test_tools_listed():
    names = {t.name for t in await server().list_tools()}
    assert names == {
        "get_video_metadata",
        "list_transcript_languages",
        "get_transcript",
        "get_video",
    }


async def test_paging_reassembles_transcript():
    mcp = server()
    first = await call(mcp, "get_transcript", url_or_id=URL, max_chars=100)
    parts, page = [first["text"]], first
    while page["next_offset"] is not None:
        page = await call(
            mcp, "get_transcript", url_or_id=URL, offset=page["next_offset"], max_chars=100
        )
        parts.append(page["text"])
    assert " ".join(parts) == " ".join(w for _, w in WORDS)
    assert first["total_chars"] == len(" ".join(w for _, w in WORDS))


async def test_get_video_without_transcript():
    result = await call(server([]), "get_video", url_or_id=URL)
    assert result["metadata"]["title"] == "T"
    assert result["transcript"] is None
    assert result["transcript_error"].startswith("no_transcript:")


async def test_invalid_id_is_tool_error():
    with pytest.raises(ToolError, match="invalid_video_id:"):
        await server().call_tool("get_transcript", {"url_or_id": "https://example.com"})


async def test_list_languages():
    result = await call(
        server([FakeTrack("en"), FakeTrack("sv", generated=True)]),
        "list_transcript_languages",
        url_or_id=URL,
    )
    assert result == [
        {"language": "en", "language_name": "EN", "is_generated": False},
        {"language": "sv", "language_name": "SV", "is_generated": True},
    ]
