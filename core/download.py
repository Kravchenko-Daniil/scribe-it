"""Download audio from YouTube / direct URL / local file; re-encode to Opus."""
from __future__ import annotations

import asyncio
import dataclasses
import os
import pathlib
import re
import sys
import time

import requests

# Yandex.Disk public API endpoint for resolving a direct download href.
API_DOWNLOAD = "https://cloud-api.yandex.net/v1/disk/public/resources/download"

# cookies.txt for yt-dlp. Env-overridable; default is the repo root (parent of
# core/), not core/ itself — otherwise the file migrates with the package and
# download_youtube silently loses cookie-gated YouTube videos.
COOKIES_PATH = pathlib.Path(
    os.environ.get("COOKIES_PATH")
    or (pathlib.Path(__file__).resolve().parent.parent / "cookies.txt")
)

# Cookies that gate an authenticated YouTube/Google session. SID/HSID/SSID are
# the classic Google session triplet, SAPISID signs authenticated API calls,
# and the __Secure-* names are their HTTPS-only successors used by current
# Google login. Once every one of these has expired the session is dead even
# though cookies.txt still exists — that's the "protuhli" signal /health needs.
SESSION_COOKIE_NAMES = frozenset({
    "SID", "HSID", "SSID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
})


@dataclasses.dataclass(frozen=True)
class CookieStatus:
    """Filesystem-only snapshot of cookies.txt health (no network calls)."""

    present: bool  # cookies.txt exists
    age_seconds: float | None  # file mtime age; None if absent
    expires_at: int | None  # earliest expiry among session cookies (epoch seconds); None if absent/unreadable
    expired: bool  # True if absent, unreadable, or every session cookie has already expired


def _read_netscape_expiries(path: pathlib.Path) -> list[tuple[str, int]]:
    """Parse a Netscape-format cookie file into (name, expiry) pairs.

    Fields are tab-separated: domain, flag, path, secure, expiration, name, value
    (expiration is the 5th field, a unix timestamp). Malformed lines — too few
    fields, a non-integer expiration — are skipped rather than raising, since a
    hand-edited or partially-exported cookies.txt is a real, recoverable case.
    """
    expiries: list[tuple[str, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        try:
            expiry = int(fields[4])
        except ValueError:
            continue
        expiries.append((fields[5], expiry))
    return expiries


def read_cookie_status(path: pathlib.Path | None = None) -> CookieStatus:
    """Read cookies.txt off disk and report whether the YouTube session is alive.

    Cheap and side-effect-free: stat + read one file, no yt-dlp, no HTTP.
    """
    p = path or COOKIES_PATH
    if not p.exists():
        return CookieStatus(present=False, age_seconds=None, expires_at=None, expired=True)
    age_seconds = time.time() - p.stat().st_mtime
    session_expiries = [
        expiry for name, expiry in _read_netscape_expiries(p) if name in SESSION_COOKIE_NAMES
    ]
    if not session_expiries:
        # File exists but carries none of the session cookies — unreadable/foreign
        # file, so there is no live session to report; treat as expired.
        return CookieStatus(present=True, age_seconds=age_seconds, expires_at=None, expired=True)
    expires_at = min(session_expiries)
    return CookieStatus(
        present=True,
        age_seconds=age_seconds,
        expires_at=expires_at,
        expired=expires_at <= int(time.time()),
    )


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
    template = str(out_dir / "%(title)s.%(ext)s")
    cookies = COOKIES_PATH

    base_cmd = [
        *YT_DLP,
        "--no-playlist",
        "-f", "bestaudio/best",
        # mweb client returns direct https media URLs (not HLS segments).
        # Default tv client serves m3u8, which googlevideo 403s from datacenter IPs.
        "--extractor-args", "youtube:player_client=mweb",
        "--extract-audio",
        "--audio-format", "opus",
        "--audio-quality", "0",
        "--remote-components", "ejs:github",
        "-o", template,
    ]
    if cookies.exists():
        base_cmd += ["--cookies", str(cookies)]
    # If no cookies — rely on bgutil PO token provider (plugin loaded from venv).

    code, log = await _run(base_cmd + [url])
    if code != 0:
        raise RuntimeError(f"yt-dlp failed: {log[-800:]}")
    for p in out_dir.iterdir():
        if p.suffix == ".opus":
            return p
    raise RuntimeError("yt-dlp succeeded but no .opus file found")


async def download_direct(url: str, out_dir: pathlib.Path) -> pathlib.Path:
    """Download arbitrary URL via yt-dlp (handles Yandex.Disk, Google Drive, direct http)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "%(title)s.%(ext)s")
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


async def has_audio(path: pathlib.Path) -> bool:
    """Return True if file contains at least one audio stream."""
    code, log = await _run([
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return code == 0 and log.strip() == "audio"


async def extract_audio(
    src: pathlib.Path, out_dir: pathlib.Path, stem: str | None = None
) -> pathlib.Path:
    """ffmpeg: extract audio track as opus (small, fast, high quality for speech)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if not await has_audio(src):
        raise RuntimeError("в файле нет аудиодорожки — нечего расшифровывать.")
    dst = out_dir / f"{stem or src.stem or 'audio'}.opus"
    code, log = await _run([
        "ffmpeg", "-y",
        "-i", str(src),
        "-vn",
        "-c:a", "libopus",
        "-b:a", "32k",
        "-ac", "1",
        # Deterministic bytes: fix the Ogg bitstream serial number (ffmpeg
        # randomizes it per mux by default) and the encoder vendor string, so
        # sha256(opus) is stable across re-encodes of identical audio. Without
        # this the Scribe cache (key = sha256(opus), core/cache.py) can NEVER
        # hit and every repeat re-pays ElevenLabs.
        "-fflags", "+bitexact",
        "-flags:a", "+bitexact",
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


def get_download_url(public_key: str, path: str) -> str:
    r = requests.get(
        API_DOWNLOAD, params={"public_key": public_key, "path": path}, timeout=60
    )
    r.raise_for_status()
    return r.json()["href"]


def download_to(url: str, dst: pathlib.Path) -> None:
    with requests.get(url, stream=True, timeout=60 * 60) as r:
        r.raise_for_status()
        with dst.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
