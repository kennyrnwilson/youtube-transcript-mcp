# youtube-transcript-mcp

An MCP server that fetches a YouTube video's metadata and transcript, so an AI
session can work with what was said, and cuts frames from the video, so it can
show what was done. It runs on the ingress VM of the
personal-portal system and reaches every client through the
`personal-mcp-portal` connector.

Design and deployment: [`docs/system-architecture/mcp-topology.md`](https://github.com/kennyrnwilson/personal-portal/blob/main/docs/system-architecture/mcp-topology.md)
in personal-portal. The deploy script is
[`deploy/ingress-vm/youtube-transcript-mcp.sh`](https://github.com/kennyrnwilson/personal-portal/blob/main/deploy/ingress-vm/youtube-transcript-mcp.sh).

The logic was copied from
[resource-librarian](https://github.com/kennyrnwilson/resource-librarian) at
commit `6674bdd`. This repo does not depend on it.

## Tools

| Tool | Returns |
|---|---|
| `get_video_metadata(url_or_id)` | title, channel, channel ID, publish date, duration, description, and `source`: `data-api` or `oembed` |
| `list_transcript_languages(url_or_id)` | each transcript track: language code, name, and whether it is auto-generated |
| `get_transcript(url_or_id, language="en", timestamps=False, offset=0, max_chars=60000)` | one page of transcript text, the language used, `total_chars`, and `next_offset` (null on the last page) |
| `get_video(url_or_id, language="en", max_chars=60000)` | metadata plus the first transcript page, in one call |
| `get_contact_sheet(url_or_id, start="0:00", end=None, fps=2.0)` | a grid of up to 64 frames, each labelled with its time, shown inline, plus a signed link to the full-size sheet |
| `get_frames(url_or_id, times, preview=True)` | up to 12 full-resolution frames: for each, its file name `frame-<32 hex>.jpg`, a signed download link and an inline preview |

Pages break on whitespace, never inside a word. The language fallback is: the
requested language, manual English, generated English, then the first track
that fetches. Fetched transcripts stay in memory for an hour, so paging through
one asks YouTube once.

A tool error's message contains one of these codes:

| Code | Meaning |
|---|---|
| `invalid_video_id` | the input is not a YouTube URL or video ID |
| `video_unavailable` | no public video has that ID |
| `no_transcript` | the video has no transcript in any language |
| `blocked` | YouTube refused the request. An operations fault, not a property of the video |

## Frames

The first frame tool call for a video downloads it with `yt-dlp`, video track
only, 720p at most. Frames need no audio, and YouTube offers video-only streams
to a client without a JavaScript runtime, which a combined stream is not.
`ffmpeg` cuts the frames. Videos longer than `YOUTUBE_MCP_MAX_VIDEO_SECONDS`
are refused before downloading.

Everything lives under `YOUTUBE_MCP_DATA_DIR`:

- `videos/<id>.<ext>`: the video, kept until the cleanup timer deletes it
- `frames/frame-<32 hex>.jpg` and `frames/sheet-<32 hex>.jpg`: named from
  the video and the times, so a repeat request is a cache hit

image-gen-mcp reads `frames/` (it never writes it), so a frame's file name or
link is a valid `source` for its `edit_image`. Files are written mode `0640`
for that reason: the directory's group is shared with image-gen and nginx.

Download links are signed for nginx's `secure_link` module, exactly as
image-gen-mcp's are: `?exp=<unix>&sig=base64url(MD5(<exp><uri><secret>))`. With
no signing secret configured, the tools return inline images only.

`get_video` does not fail on `no_transcript` or `blocked`: it returns the
metadata with `transcript: null` and the reason in `transcript_error`.

## Configuration

| Variable | Use |
|---|---|
| `YOUTUBE_API_KEY_FILE` | path to a file that holds the YouTube Data API v3 key (the deployed form) |
| `YOUTUBE_API_KEY` | the key itself, read only when `YOUTUBE_API_KEY_FILE` is unset (local runs) |
| `YOUTUBE_MCP_DATA_DIR` | where videos and frames are kept; default `/var/lib/youtube-mcp` |
| `YOUTUBE_MCP_MAX_VIDEO_SECONDS` | the longest video the frame tools download; default `3600` |
| `YOUTUBE_FRAMES_URL_SECRET_FILE` | path to the frame-link signing secret (the deployed form) |
| `YOUTUBE_FRAMES_URL_SECRET` | the secret itself, for local runs |
| `YOUTUBE_MCP_PUBLIC_BASE` | the origin of signed links; default `https://gateway.kennyrnwilson.com` |

With no key, or a key the Data API rejects, metadata comes from YouTube's
keyless oEmbed endpoint, which has only the title and the channel name.
Transcripts never need a key.

## Run

```zsh
uv sync --extra dev
uv run pytest
uv run python -m youtube_transcript_mcp --selftest       # 0 OK, 1 fault, 2 blocked by YouTube
uv run python -m youtube_transcript_mcp                  # stdio
uv run python -m youtube_transcript_mcp --transport streamable-http --port 5106
```

`--selftest` fetches the metadata and transcript of one known video, and checks
that `ffmpeg` is installed. If a key is configured, it also fails when the Data
API does not accept it. The frame tools need `ffmpeg` on `PATH`.
