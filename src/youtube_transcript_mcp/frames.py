"""Video frames: download a video once, then cut frames and contact sheets from it.

A contact sheet is a grid of frames, each labelled with its time, for choosing
which moments to illustrate. A frame is one full-resolution still.

Files are named from what they contain, so a repeat request is a cache hit:

- ``videos/<video_id>.<ext>``            the downloaded video, video track only
- ``frames/frame-<32 hex>.jpg``          one frame; image-gen's ``edit_image``
                                         reads this directory and takes this name
- ``frames/sheet-<32 hex>.jpg``          one contact sheet

Only the video track is downloaded. Frames need no audio, and YouTube offers
video-only streams up to 540p to a client without a JavaScript runtime, which a
combined stream at that size is not.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from youtube_transcript_mcp.errors import YouTubeError

VIDEO_FORMAT = "bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]"
MAX_SHEET_CELLS = 64
SHEET_CELL_WIDTH = 320
DEFAULT_MAX_DURATION = 3600
FILE_MODE = 0o640


# --- times -------------------------------------------------------------------


def parse_time(text: str | float) -> float:
    """Seconds from 83.5, '83.5', '1:23.5' or '1:01:23'."""
    if isinstance(text, (int, float)):
        seconds = float(text)
    else:
        parts = str(text).strip().split(":")
        if not 1 <= len(parts) <= 3 or any(p.strip() == "" for p in parts):
            raise ValueError(f"not a time: {text!r}")
        try:
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + float(part)
        except ValueError:
            raise ValueError(f"not a time: {text!r}") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"not a time: {text!r}")
    return seconds


def label_time(seconds: float) -> str:
    """'m:ss', with tenths when not a whole second; 'h:mm:ss' past an hour."""
    whole = int(seconds)
    tenths = round((seconds - whole) * 10)
    if tenths == 10:
        whole, tenths = whole + 1, 0
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    return f"{text}.{tenths}" if tenths else text


def sheet_times(
    start: float, end: float, fps: float, max_cells: int = MAX_SHEET_CELLS
) -> list[float]:
    """Evenly spaced times from start to end inclusive, thinned to max_cells."""
    if end <= start:
        raise ValueError("end must be after start")
    if fps <= 0:
        raise ValueError("fps must be positive")
    step = 1 / fps
    count = int((end - start) / step + 1e-9) + 1
    if count > max_cells:
        step = (end - start) / (max_cells - 1)
        count = max_cells
    return [round(start + i * step, 2) for i in range(count)]


def grid_shape(count: int) -> tuple[int, int]:
    """(columns, rows): up to 8 columns of 16:9 cells."""
    cols = min(8, max(1, count))
    return cols, -(-count // cols)


def _digest(*parts: Any) -> str:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()[:32]


def frame_name(video_id: str, seconds: float) -> str:
    return f"frame-{_digest('frame', video_id, f'{seconds:.2f}')}.jpg"


def sheet_name(video_id: str, times: list[float]) -> str:
    return f"sheet-{_digest('sheet', video_id, *(f'{t:.2f}' for t in times))}.jpg"


# --- side effects ------------------------------------------------------------


class Downloader(Protocol):
    def info(self, video_id: str) -> dict[str, Any]: ...

    def download(self, video_id: str, dest_dir: Path) -> Path: ...


def _classify(exc: Exception) -> YouTubeError:
    text = str(exc)
    if "confirm you" in text and "bot" in text or "HTTP Error 429" in text:
        return YouTubeError("blocked", f"YouTube refused the download: {text}")
    if any(s in text for s in ("unavailable", "Private video", "removed", "not available")):
        return YouTubeError("video_unavailable", text)
    return YouTubeError("blocked", f"download failed: {text}")


class YtDlpDownloader:
    """yt-dlp, video track only, 720p at most."""

    def _options(self, **extra: Any) -> dict[str, Any]:
        return {"quiet": True, "no_warnings": True, "noprogress": True, **extra}

    def info(self, video_id: str) -> dict[str, Any]:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError

        try:
            with YoutubeDL(self._options(format=VIDEO_FORMAT)) as ydl:
                return ydl.extract_info(_url(video_id), download=False)
        except DownloadError as e:
            raise _classify(e) from e

    def download(self, video_id: str, dest_dir: Path) -> Path:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError

        outtmpl = str(dest_dir / f"{video_id}.%(ext)s")
        try:
            with YoutubeDL(self._options(format=VIDEO_FORMAT, outtmpl=outtmpl)) as ydl:
                info = ydl.extract_info(_url(video_id), download=True)
                return Path(ydl.prepare_filename(info))
        except DownloadError as e:
            raise _classify(e) from e


def _url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def grab_frame(video: Path, seconds: float, *, width: int | None = None) -> bytes:
    """One JPEG frame from `video` at `seconds`, optionally scaled to `width`."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")
    command = [ffmpeg, "-loglevel", "error", "-ss", f"{seconds}", "-i", str(video)]
    command += ["-frames:v", "1", "-q:v", "3"]
    if width:
        command += ["-vf", f"scale={width}:-2"]
    command += ["-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    result = subprocess.run(command, capture_output=True, check=False, timeout=60)
    if result.returncode != 0 or not result.stdout:
        raise ValueError(f"no frame at {label_time(seconds)}; is it past the end of the video?")
    return result.stdout


def build_sheet(cells: list[tuple[float, bytes]]) -> bytes:
    """A labelled grid of JPEG frames, as one JPEG."""
    from PIL import Image, ImageDraw, ImageFont

    images = [(t, Image.open(io.BytesIO(data)).convert("RGB")) for t, data in cells]
    cell_w, cell_h = images[0][1].size
    cols, rows = grid_shape(len(images))
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "black")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=22)
    for i, (t, image) in enumerate(images):
        x, y = (i % cols) * cell_w, (i // cols) * cell_h
        sheet.paste(image.resize((cell_w, cell_h)), (x, y))
        label = label_time(t)
        box = draw.textbbox((x + 6, y + 4), label, font=font)
        draw.rectangle((box[0] - 4, box[1] - 2, box[2] + 4, box[3] + 2), fill="black")
        draw.text((x + 6, y + 4), label, fill="yellow", font=font)
    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=85)
    return out.getvalue()


def shrink(data: bytes, width: int) -> bytes:
    """`data` scaled down to at most `width` pixels wide, as JPEG."""
    from PIL import Image

    image = Image.open(io.BytesIO(data)).convert("RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=80)
    return out.getvalue()


def write_file(path: Path, data: bytes) -> Path:
    """Write atomically, readable by the directory's group (nginx and image-gen)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


@dataclass
class Sheet:
    path: Path
    times: list[float]


class FrameStore:
    """Videos and the frames and sheets cut from them, under one root."""

    def __init__(
        self,
        root: Path,
        downloader: Downloader | None = None,
        *,
        max_duration: int = DEFAULT_MAX_DURATION,
        grab=grab_frame,
    ) -> None:
        self.videos = root / "videos"
        self.frames = root / "frames"
        self.downloader = downloader or YtDlpDownloader()
        self.max_duration = max_duration
        self.grab = grab

    def video(self, video_id: str) -> tuple[Path, float]:
        """The cached video and its duration, downloading it first if needed."""
        for path in self.videos.glob(f"{video_id}.*"):
            if not path.name.startswith(".") and path.suffix not in (".part", ".duration"):
                path.touch()
                return path, self._duration(path)
        info = self.downloader.info(video_id)
        duration = float(info.get("duration") or 0)
        if duration > self.max_duration:
            raise ValueError(
                f"the video is {label_time(duration)} long; the limit is "
                f"{label_time(self.max_duration)}"
            )
        self.videos.mkdir(parents=True, exist_ok=True)
        path = self.downloader.download(video_id, self.videos)
        write_duration(path, duration)
        return path, duration

    def _duration(self, video: Path) -> float:
        marker = video.with_name(video.name + ".duration")
        try:
            return float(marker.read_text())
        except (OSError, ValueError):
            return 0.0

    def frame(self, video_id: str, seconds: float) -> Path:
        path = self.frames / frame_name(video_id, seconds)
        if path.is_file():
            path.touch()
            return path
        video, duration = self.video(video_id)
        if duration and seconds > duration:
            raise ValueError(
                f"{label_time(seconds)} is past the end of the video ({label_time(duration)})"
            )
        return write_file(path, self.grab(video, seconds))

    def sheet(self, video_id: str, start: float, end: float | None, fps: float) -> Sheet:
        video, duration = self.video(video_id)
        if end is None or (duration and end > duration):
            end = duration if duration else end
        if end is None:
            raise ValueError("give an end time: the video's length is unknown")
        times = sheet_times(start, end, fps)
        path = self.frames / sheet_name(video_id, times)
        if path.is_file():
            path.touch()
            return Sheet(path, times)
        cells = []
        for t in times:
            try:
                cells.append((t, self.grab(video, t, width=SHEET_CELL_WIDTH)))
            except ValueError:
                break
        if not cells:
            raise ValueError("no frames in that range")
        return Sheet(write_file(path, build_sheet(cells)), [t for t, _ in cells])


def write_duration(video: Path, duration: float) -> None:
    video.with_name(video.name + ".duration").write_text(f"{duration}")
