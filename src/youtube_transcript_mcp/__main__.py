"""Entry point: `python -m youtube_transcript_mcp` (stdio server) or `--selftest`."""

from __future__ import annotations

import argparse
import os
import sys
import traceback

from youtube_transcript_mcp.errors import YouTubeError
from youtube_transcript_mcp.metadata import MetadataClient, read_api_key
from youtube_transcript_mcp.server import build_server
from youtube_transcript_mcp.transcript import TranscriptClient

# "Me at the zoo", the first video on YouTube: public since 2005, with captions.
SELFTEST_VIDEO = "jNQXAC9IVRw"

EXIT_FAIL = 1
EXIT_BLOCKED = 2


def _selftest(video_id: str) -> int:
    """Fetch one known video's metadata and transcript. Exit 2 if YouTube blocks
    the request, so an IP block is not mistaken for a code fault."""
    try:
        meta = MetadataClient(read_api_key()).fetch(video_id)
        print(f"metadata   OK  source={meta['source']} title={meta['title']!r}")
        if os.environ.get("YOUTUBE_API_KEY_FILE") and meta["source"] != "data-api":
            print(f"selftest FAIL: API key set but not used: {meta.get('note')}", file=sys.stderr)
            return EXIT_FAIL
        transcript = TranscriptClient().fetch(video_id)
        text = transcript.render()
        print(f"transcript OK  language={transcript.track.language_code} chars={len(text)}")
        return 0
    except YouTubeError as e:
        status = "BLOCKED" if e.code == "blocked" else "FAIL"
        print(f"selftest {status}: {e}", file=sys.stderr)
        return EXIT_BLOCKED if e.code == "blocked" else EXIT_FAIL
    except Exception:  # noqa: BLE001 - report any fault
        print("selftest FAIL:", file=sys.stderr)
        traceback.print_exc()
        return EXIT_FAIL


def main() -> int:
    parser = argparse.ArgumentParser(prog="youtube-transcript-mcp")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Fetch one known video; exit 0 OK, 1 fault, 2 blocked by YouTube.",
    )
    parser.add_argument("--selftest-video", default=SELFTEST_VIDEO, help=argparse.SUPPRESS)
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "streamable-http"],
        help="Transport to use (default: stdio).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for streamable-http.")
    parser.add_argument("--port", type=int, default=5106, help="Bind port for streamable-http.")
    args = parser.parse_args()

    if args.selftest:
        return _selftest(args.selftest_video)

    server = build_server(host=args.host, port=args.port)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
