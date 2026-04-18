"""Per-request working directories under tmp/ with automatic cleanup."""
from __future__ import annotations

import pathlib
import shutil
import time
import uuid

ROOT = pathlib.Path(__file__).parent / "tmp"


def new_workdir() -> pathlib.Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    d = ROOT / f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup(path: pathlib.Path) -> None:
    if path.exists() and path.is_relative_to(ROOT):
        shutil.rmtree(path, ignore_errors=True)


def cleanup_old(max_age_hours: float = 24.0) -> int:
    if not ROOT.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for p in ROOT.iterdir():
        if p.is_dir() and p.stat().st_mtime < cutoff:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    return removed
