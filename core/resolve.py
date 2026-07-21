"""Resolve a source (URL or local path) into a list of MediaItem — БЕЗ скачивания.

Классификатор (паритет с сегодняшним монолитом-ботом + разворот плейлиста/папки):

  YouTube ролик    → 1 MediaItem kind="youtube"   (без сети)
  YouTube плейлист  → N MediaItem kind="youtube"   (yt-dlp --flat-playlist)
  Яндекс папка      → N MediaItem kind="yandex"    (cloud-api, включая папку из 1 файла)
  Яндекс файл       → 1 MediaItem kind="direct"    (yt-dlp, как сегодня)
  Drive / прямой http → 1 MediaItem kind="direct"
  Локальный путь     → 1 MediaItem kind="file"

Дискриминатор папка↔файл для Яндекса — НАЛИЧИЕ `_embedded` в ответе
public/resources (type=="dir" vs "file"), а не число файлов. Весь публичный URL
целиком передаётся как public_key (паритет с yadisk_batch.py). Скачивание НЕ здесь:
им владеет job-loop (download по kind).
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlparse

import requests

from core.download import YT_DLP, is_url, is_youtube_url

API_LIST = "https://cloud-api.yandex.net/v1/disk/public/resources"

INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

# Публичные ссылки Яндекс.Диска: disk.yandex.<tld>/d/<id> (папка/файл), /i/<id>,
# а также короткий домен yadi.sk/d|i/<id>. Весь URL уходит как public_key.
YANDEX_RE = re.compile(
    r"^https?://(disk\.yandex\.[a-z.]+|yadi\.sk)/[di]/",
    re.IGNORECASE,
)


@dataclass
class MediaItem:
    index: int  # 0-based позиция в итоговом списке (проставляется в _finalize)
    name: str  # человекочитаемое имя (может нести исходное расширение, напр. "1.mp4")
    kind: Literal["youtube", "yandex", "direct", "file"]
    ref: str  # url ролика / сериализованный (public_key, path) для yandex / абс. local_path
    stem: str  # уникальный per-item stem; имя выдаваемого .txt = "<stem>.txt"


# --- slugify / listing (портировано из yadisk_batch.py:33-47) ----------------

def slugify(name: str) -> str:
    stem = pathlib.Path(name).stem
    s = INVALID_FS.sub(" ", stem).strip().strip(".")
    s = re.sub(r"\s+", "-", s)
    return s[:120] or "audio"


def _yandex_meta(public_key: str) -> dict:
    """Сырой ответ public/resources; весь публичный URL — это public_key."""
    r = requests.get(API_LIST, params={"public_key": public_key, "limit": 200}, timeout=60)
    r.raise_for_status()
    return r.json()


def _files_of(body: dict) -> list[dict]:
    items = body.get("_embedded", {}).get("items", [])
    return [i for i in items if i.get("type") == "file"]


def list_folder(public_key: str) -> list[dict]:
    """Файлы (type=='file') внутри публичной папки Яндекс.Диска (yadisk_batch.py:43-47)."""
    return _files_of(_yandex_meta(public_key))


# --- URL sniffs --------------------------------------------------------------

def is_yandex_url(text: str) -> bool:
    return bool(YANDEX_RE.match(text.strip()))


def _name_from_url(url: str) -> str:
    parsed = urlparse(url)
    tail = pathlib.PurePosixPath(parsed.path).name
    return tail or parsed.netloc or url


# --- YouTube -----------------------------------------------------------------

def _is_youtube_playlist(url: str) -> bool:
    """Плейлист = /playlist ЛИБО list= без v= (watch?v=..&list=.. → одиночный, как --no-playlist)."""
    parsed = urlparse(url)
    if "playlist" in parsed.path.lower():
        return True
    qs = parse_qs(parsed.query)
    return "list" in qs and "v" not in qs


def _youtube_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        seg = parsed.path.lstrip("/").split("/")[0]
        return seg or None
    qs = parse_qs(parsed.query)
    if qs.get("v"):
        return qs["v"][0]
    m = re.match(r"/(?:shorts|embed|live|v)/([^/?#]+)", parsed.path)
    return m.group(1) if m else None


def _flat_playlist_ids(url: str) -> list[str]:
    """Развернуть плейлист в id роликов через `yt-dlp --flat-playlist` (сеть)."""
    proc = subprocess.run(
        [*YT_DLP, "--flat-playlist", "--print", "%(id)s", url],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"yt-dlp --flat-playlist failed: {(proc.stderr or proc.stdout)[-600:]}"
        )
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


# --- per-kind builders -------------------------------------------------------

def _resolve_youtube(url: str) -> list[MediaItem]:
    if _is_youtube_playlist(url):
        out: list[MediaItem] = []
        for vid in _flat_playlist_ids(url):
            ref = f"https://www.youtube.com/watch?v={vid}"
            out.append(MediaItem(0, vid, "youtube", ref, slugify(vid)))
        return out
    # Одиночный ролик — без сети: имя из URL, ref = исходный url (download_youtube --no-playlist).
    vid = _youtube_id(url)
    name = vid or _name_from_url(url)
    return [MediaItem(0, name, "youtube", url, slugify(name))]


def _resolve_yandex(url: str) -> list[MediaItem]:
    body = _yandex_meta(url)
    if "_embedded" in body:  # type=="dir" → cloud-api для ВСЕХ файлов (вкл. папку из 1 файла)
        out: list[MediaItem] = []
        for item in _files_of(body):
            name = item["name"]
            path = item.get("path") or f"/{name}"
            ref = json.dumps({"public_key": url, "path": path}, ensure_ascii=False)
            out.append(MediaItem(0, name, "yandex", ref, slugify(name)))
        return out
    # type=="file" (нет _embedded) → одиночный файл → direct через yt-dlp (как сегодня).
    name = body.get("name") or _name_from_url(url)
    return [MediaItem(0, name, "direct", url, slugify(name))]


def _resolve_direct(url: str) -> list[MediaItem]:
    name = _name_from_url(url)
    return [MediaItem(0, name, "direct", url, slugify(name))]


def _resolve_file(path: str) -> list[MediaItem]:
    p = pathlib.Path(path).expanduser().resolve()
    return [MediaItem(0, p.name, "file", str(p), slugify(p.name))]


def _finalize(items: list[MediaItem]) -> list[MediaItem]:
    """Проставить 0-based index; при коллизии stem — добавить индекс."""
    used: set[str] = set()
    for i, it in enumerate(items):
        it.index = i
        if it.stem in used:
            it.stem = f"{it.stem}-{i}"
        used.add(it.stem)
    return items


def resolve(source: str) -> list[MediaItem]:
    """Источник (URL или локальный путь) → list[MediaItem], БЕЗ скачивания."""
    src = source.strip()
    if is_youtube_url(src):
        items = _resolve_youtube(src)
    elif is_yandex_url(src):
        items = _resolve_yandex(src)
    elif is_url(src):
        items = _resolve_direct(src)
    else:
        items = _resolve_file(src)
    return _finalize(items)
