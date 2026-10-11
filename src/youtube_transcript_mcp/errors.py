"""The error codes a caller can act on.

Each needs a different response from the caller, so they stay distinct:

- ``invalid_video_id``  the input is not a YouTube URL or video ID; fix the input.
- ``video_unavailable`` the ID is well formed but no public video has it.
- ``no_transcript``     the video exists but has no transcript in any language.
- ``blocked``           YouTube refused the request. An operations fault, not a
                        property of the video; retrying later or from another IP helps.
"""

from __future__ import annotations

from typing import Literal

Code = Literal["invalid_video_id", "video_unavailable", "no_transcript", "blocked"]


class YouTubeError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
