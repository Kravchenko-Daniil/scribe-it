"""Download audio from YouTube / direct URL / local file; re-encode to Opus."""
from __future__ import annotations

import asyncio
import pathlib
import re
import sys

YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/",
    re.IGNORECASE,
)
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_RE.match(text.strip()))


def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


YT_DLP = [sys.executable, "-m", "yt_dlp"]


async def download_youtube(url: str, out_dir: pathlib.Path) -> pathlib.Path:
    """yt-dlp: audio-only, re-encoded to opus. Returns path to the file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "source.%(ext)s")
    code, log = await _run([
        *YT_DLP,
        "--no-playlist",
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "opus",
        "--audio-quality", "0",
        "-o", template,
        url,
    ])
    if code != 0:
        raise RuntimeError(f"yt-dlp failed: {log[-600:]}")
    for p in out_dir.iterdir():
        if p.suffix == ".opus":
            return p
    raise RuntimeError("yt-dlp succeeded but no .opus file found")


async def download_direct(url: str, out_dir: pathlib.Path) -> pathlib.Path:
    """Download arbitrary URL via yt-dlp (handles Yandex.Disk, Google Drive, direct http)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "source.%(ext)s")
    code, log = await _run([
        *YT_DLP,
        "--no-playlist",
        "-o", template,
        url,
    ])
    if code != 0:
        raise RuntimeError(f"Direct download failed: {log[-600:]}")
    for p in sorted(out_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and p.stat().st_size > 0:
            return p
    raise RuntimeError("downloaded file not found")


async def extract_audio(src: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    """ffmpeg: extract audio track as opus (small, fast, high quality for speech)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "audio.opus"
    code, log = await _run([
        "ffmpeg", "-y",
        "-i", str(src),
        "-vn",
        "-c:a", "libopus",
        "-b:a", "32k",
        "-ac", "1",
        str(dst),
    ])
    if code != 0:
        raise RuntimeError(f"ffmpeg failed: {log[-600:]}")
    return dst


async def probe_duration(path: pathlib.Path) -> float | None:
    """Return duration in seconds via ffprobe, or None if unavailable."""
    code, log = await _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    if code != 0:
        return None
    try:
        return float(log.strip().split("\n")[0])
    except (ValueError, IndexError):
        return None
