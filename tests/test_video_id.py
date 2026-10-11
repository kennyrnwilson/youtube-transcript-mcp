import pytest

from youtube_transcript_mcp.errors import YouTubeError
from youtube_transcript_mcp.video_id import extract_video_id, require_video_id

ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        # Cases copied from resource-librarian.
        f"https://www.youtube.com/watch?v={ID}",
        f"https://youtu.be/{ID}",
        f"https://www.youtube.com/embed/{ID}",
        f"https://www.youtube.com/watch?v={ID}&feature=share",
        ID,
        # New URL forms.
        f"https://www.youtube.com/shorts/{ID}",
        f"https://youtube.com/live/{ID}?feature=share",
        f"https://m.youtube.com/watch?v={ID}",
        f"https://music.youtube.com/watch?v={ID}&list=RD",
        f"https://www.youtube.com/watch?feature=share&v={ID}",
        f"https://youtu.be/{ID}?si=abc&t=42",
        f"www.youtube.com/watch?v={ID}",
        f"  {ID}  ",
    ],
)
def test_extracts_id(url):
    assert extract_video_id(url) == ID


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-valid-url",
        "https://example.com",
        "short-id",
        f"https://example.com/watch?v={ID}",
        "https://www.youtube.com/watch?v=tooshort",
        "https://www.youtube.com/@somechannel",
        f"https://www.youtube.com/watch?v={ID}x",
    ],
)
def test_rejects(bad):
    assert extract_video_id(bad) is None


def test_require_raises_invalid_video_id():
    with pytest.raises(YouTubeError) as e:
        require_video_id("https://example.com")
    assert e.value.code == "invalid_video_id"
