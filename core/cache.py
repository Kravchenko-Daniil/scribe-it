"""Scribe-step кеш: сырой ответ ElevenLabs по ключу sha256(opus) + параметры запроса.

Кешируется единственный оплачиваемый шаг — Scribe. Ключ = sha256 *opus* (выход
ffmpeg, не источник) + параметры, меняющие ответ Scribe (язык, число спикеров,
biased_keywords). Display-имена спикеров применяются на рендере и в ключ НЕ входят.
TTL — 30 дней по mtime: проверяется на чтении и продлевается (скользящий) на хите.

Хеширование в key() синхронное; обёртка в asyncio.to_thread — забота вызывающего
(api.py, Фаза 2), здесь НЕ делается.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time

CACHE_DIR = pathlib.Path(
    os.environ.get("SCRIBE_CACHE_DIR")
    or (pathlib.Path(__file__).resolve().parent.parent / "cache")
)

TTL_SECONDS = 30 * 24 * 3600  # 30 дней
_CHUNK = 1024 * 1024


def _sha256_file(path: pathlib.Path) -> str:
    """sha256 полных байт файла, потоково по чанкам, hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def key(
    opus_path: pathlib.Path | str,
    language: str | None,
    num_speakers: int | None,
    biased: list[str] | None,
) -> str:
    """Ключ кеша = "<opus_hash>_<param_hash>". biased приходит готовым списком."""
    opus_hash = _sha256_file(pathlib.Path(opus_path))
    param_blob = json.dumps(
        {
            "language": language,
            "num_speakers": num_speakers,
            "biased": sorted(biased or []),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    param_hash = hashlib.sha256(param_blob.encode()).hexdigest()
    return f"{opus_hash}_{param_hash}"


def _path_for(cache_key: str) -> pathlib.Path:
    return CACHE_DIR / f"{cache_key}.json"


def get(key: str) -> dict | None:
    """Сырой Scribe-ответ или None. TTL по mtime на чтении; на хите продлить mtime."""
    p = _path_for(key)
    try:
        age = time.time() - p.stat().st_mtime
    except FileNotFoundError:
        return None
    if age > TTL_SECONDS:
        return None  # протухло → промах (физическое удаление — забота evict())
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        os.utime(p, None)  # скользящий TTL: коснуться на хите
    except OSError:
        pass
    return data


def put(key: str, data: dict) -> None:
    """Атомарно записать сырой dict (.tmp + os.replace)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path_for(key)
    tmp = CACHE_DIR / f"{key}.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def evict() -> int:
    """Физически удалить файлы старше TTL, вернуть число удалённых."""
    if not CACHE_DIR.exists():
        return 0
    cutoff = time.time() - TTL_SECONDS
    removed = 0
    for p in CACHE_DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except FileNotFoundError:
            pass
    return removed
