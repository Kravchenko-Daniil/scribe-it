#!/usr/bin/env python3
"""Deterministic Phase-1 verification for scribe-it core/ migration.

Plain-python (no pytest). Run:
    cd /mnt/d/lab/products/scribe-bot && uv run python tests/check_phase1.py

Prints PASS/FAIL per check; exits non-zero if ANY check fails.
Written by a fresh verifier — it does NOT trust the builder, it proves via
imports/subprocess/git that Phase 1 of docs/PLAN.md was done and nothing was lost.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time

# --- make `core` importable regardless of cwd --------------------------------
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_failures: list[str] = []
_passes: list[str] = []


def check(cond: bool, label: str) -> bool:
    if cond:
        print(f"PASS  {label}")
        _passes.append(label)
    else:
        print(f"FAIL  {label}")
        _failures.append(label)
    return cond


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================================
# 1. IMPORT
# ============================================================================
section("1. IMPORT")
try:
    import core.download as dl  # noqa: E402
    import core.scribe as scribe  # noqa: E402
    import core.storage as storage  # noqa: E402
    import core.resolve as resolve  # noqa: E402
    import core.cache as cache  # noqa: E402
    check(True, "import core.download/scribe/storage/resolve/cache")
except Exception as e:  # pragma: no cover
    check(False, f"import core.* raised: {e!r}")
    # Without imports the rest is meaningless.
    print("\nRESULT: FAIL (imports broken)")
    sys.exit(1)


# ============================================================================
# 2. SCRIBE FROZEN — core/scribe.py byte-for-byte identical to its committed
#    HEAD state. The old scribe.py→core/scribe.py move is committed (Phase 1),
#    so parity is now asserted against HEAD:core/scribe.py: this proves the
#    render core has NOT drifted since the Phase-1 commit (a boundary that
#    stays in force through Phase 2+).
# ============================================================================
section("2. SCRIBE FROZEN (byte-for-byte vs git HEAD:core/scribe.py)")
head_scribe = subprocess.run(
    ["git", "-C", str(REPO), "show", "HEAD:core/scribe.py"],
    capture_output=True,
)
check(head_scribe.returncode == 0, "git show HEAD:core/scribe.py succeeds")
core_scribe_bytes = (REPO / "core" / "scribe.py").read_bytes()
check(
    head_scribe.stdout == core_scribe_bytes,
    "core/scribe.py identical to HEAD:core/scribe.py (empty diff)",
)


# ============================================================================
# 3. RENDER GOLDEN (runtime, deterministic)
# ============================================================================
section("3. RENDER GOLDEN (to_paragraphs / to_srt)")


def build_words() -> list[dict]:
    words: list[dict] = []
    # Paragraph 1: speaker_0
    t = 0.0
    for tok in ["Привет", ",", "как", "дела", "?"]:
        words.append({"type": "word", "text": tok, "start": t, "end": t + 0.4, "speaker_id": "speaker_0"})
        t += 0.5
    # Paragraph 2: speaker_1 (speaker change -> new paragraph)
    t = 6.0
    for tok in ["Хорошо", ",", "спасибо", "большое", "."]:
        words.append({"type": "word", "text": tok, "start": t, "end": t + 0.4, "speaker_id": "speaker_1"})
        t += 0.5
    # Paragraph 3: speaker_1 again, but after >20s pause -> new paragraph (same speaker)
    t = 40.0
    for tok in ["Прошло", "много", "времени", "."]:
        words.append({"type": "word", "text": tok, "start": t, "end": t + 0.4, "speaker_id": "speaker_1"})
        t += 0.5
    # Paragraph 4: speaker_0 long utterance for _split_long (>1500 chars)
    t = 60.0
    sentence = ["Это", "очень", "длинная", "реплика", "которая", "должна",
                "превысить", "лимит", "символов", "."]
    for _ in range(40):
        for tok in sentence:
            words.append({"type": "word", "text": tok, "start": t, "end": t + 0.3, "speaker_id": "speaker_0"})
            t += 0.35
    return words


# Golden anchors — computed once from the current (post-migration) code and
# frozen here. Any future regression in the render flips these hashes.
GOLDEN_PARA_SHA = "0557934b5a866f2de794f9943801667caa6ce2e2f0aebaf726587190ae93bcce"
GOLDEN_SRT_SHA = "258ce815939a300f81bc2669ee8833fb963fe916428639aee912e1c6eb4528bd"
GOLDEN_PARA_HEAD = "[speaker_0] Привет, как дела?\n\n[speaker_1] Хорошо, спасибо большое."

_w = build_words()
para1 = scribe.to_paragraphs(_w)
para2 = scribe.to_paragraphs(_w)
srt1 = scribe.to_srt(_w)
srt2 = scribe.to_srt(_w)

check(bool(para1) and bool(srt1), "to_paragraphs / to_srt output non-empty")
check(para1 == para2 and srt1 == srt2, "render deterministic (two calls identical)")
para_sha = hashlib.sha256(para1.encode("utf-8")).hexdigest()
srt_sha = hashlib.sha256(srt1.encode("utf-8")).hexdigest()
check(para_sha == GOLDEN_PARA_SHA, f"to_paragraphs matches golden sha ({para_sha[:12]}…)")
check(srt_sha == GOLDEN_SRT_SHA, f"to_srt matches golden sha ({srt_sha[:12]}…)")
check(para1.startswith(GOLDEN_PARA_HEAD), "to_paragraphs golden head string matches verbatim")
# Speaker split present: both labels appear and >=4 paragraph breaks (split by
# speaker change, >20s pause, and _split_long on the long utterance -> 5 paras).
check("[speaker_0]" in para1 and "[speaker_1]" in para1, "paragraphs split by speaker (both labels present)")
check(para1.count("\n\n") + 1 == 5, "5 paragraphs (speaker + pause + _split_long all fired)")
# SRT timecodes present.
check("-->" in srt1 and re.search(r"\d{2}:\d{2}:\d{2},\d{3}", srt1) is not None, "srt has timecodes (HH:MM:SS,mmm -->)")


# ============================================================================
# 4. COOKIES PATH — resolves to <repo-root>/cookies.txt, not core/cookies.txt
# ============================================================================
section("4. COOKIES PATH")
os.environ.pop("COOKIES_PATH", None)
importlib.reload(dl)
expected_default = REPO / "cookies.txt"
check(dl.COOKIES_PATH == expected_default, f"COOKIES_PATH default == {expected_default}")
check(
    dl.COOKIES_PATH.parent == REPO and dl.COOKIES_PATH.parent.name != "core",
    "COOKIES_PATH default is repo root, NOT core/",
)
os.environ["COOKIES_PATH"] = "/x/y.txt"
importlib.reload(dl)
check(dl.COOKIES_PATH == pathlib.Path("/x/y.txt"), "COOKIES_PATH honours env override (/x/y.txt)")
os.environ.pop("COOKIES_PATH", None)
importlib.reload(dl)


# ============================================================================
# 5. STORAGE TMP-ROOT — resolves to <repo-root>/tmp, not core/tmp
# ============================================================================
section("5. STORAGE TMP-ROOT")
os.environ.pop("SCRIBE_TMP_DIR", None)
importlib.reload(storage)
expected_tmp = REPO / "tmp"
check(storage.ROOT == expected_tmp, f"storage.ROOT default == {expected_tmp}")
check(
    storage.ROOT.parent == REPO and storage.ROOT.parent.name != "core",
    "storage.ROOT default is repo root/tmp, NOT core/tmp",
)
os.environ["SCRIBE_TMP_DIR"] = "/tmp/xxx"
importlib.reload(storage)
check(storage.ROOT == pathlib.Path("/tmp/xxx"), "storage.ROOT honours env override (/tmp/xxx)")
os.environ.pop("SCRIBE_TMP_DIR", None)
importlib.reload(storage)


# ============================================================================
# 6. CACHE roundtrip + TTL
# ============================================================================
section("6. CACHE roundtrip + TTL")
cache_dir = REPO / "tests" / "_cache_tmp"
if cache_dir.exists():
    for f in cache_dir.glob("*"):
        f.unlink()
    cache_dir.rmdir()
os.environ["SCRIBE_CACHE_DIR"] = str(cache_dir)
importlib.reload(cache)
check(cache.CACHE_DIR == cache_dir, f"cache.CACHE_DIR honours env ({cache_dir})")

opus_path = cache_dir.parent / "_fixture.opus"
opus_path.write_bytes(b"OpusHead\x01\x02\x03fake-opus-bytes")

k1 = cache.key(opus_path, "ru", 2, ["Иван", "Пётр"])
k1b = cache.key(opus_path, "ru", 2, ["Иван", "Пётр"])
check(k1 == k1b, "key() deterministic (two calls identical)")
check(re.fullmatch(r"[0-9a-f]{64}_[0-9a-f]{64}", k1) is not None, 'key() format "<hex64>_<hex64>"')

k_lang = cache.key(opus_path, "en", 2, ["Иван", "Пётр"])
k_num = cache.key(opus_path, "ru", 3, ["Иван", "Пётр"])
k_biased = cache.key(opus_path, "ru", 2, ["Иван"])
check(k_lang != k1, "different language -> different key")
check(k_num != k1, "different num_speakers -> different key")
check(k_biased != k1, "different biased set -> different key")

k_reordered = cache.key(opus_path, "ru", 2, ["Пётр", "Иван"])
check(k_reordered == k1, "reordered biased -> SAME key (sorted)")

cache.put(k1, {"a": 1})
got = cache.get(k1)
check(got == {"a": 1}, "put then get roundtrips raw dict")

# TTL expiry: age the cache file 31 days -> get() returns None (miss).
cache_file = cache.CACHE_DIR / f"{k1}.json"
old = time.time() - 31 * 24 * 3600
os.utime(cache_file, (old, old))
check(cache.get(k1) is None, "get() returns None for file older than 30d TTL")

# Sliding TTL: age within window (5 days), get() returns dict AND refreshes mtime to ~now.
k2 = cache.key(opus_path, "ru", 2, ["Only"])
cache.put(k2, {"b": 2})
cache_file2 = cache.CACHE_DIR / f"{k2}.json"
aged = time.time() - 5 * 24 * 3600
os.utime(cache_file2, (aged, aged))
got2 = cache.get(k2)
mtime_after = cache_file2.stat().st_mtime
check(got2 == {"b": 2}, "sliding TTL: get() within window returns dict")
check(abs(mtime_after - time.time()) < 5, "sliding TTL: get() refreshed mtime to ~now (os.utime)")

# cleanup cache fixture
os.environ.pop("SCRIBE_CACHE_DIR", None)
try:
    opus_path.unlink()
    for f in cache_dir.glob("*"):
        f.unlink()
    cache_dir.rmdir()
except OSError:
    pass
importlib.reload(cache)


# ============================================================================
# 7. RESOLVE classifier (offline branches only, NO network)
# ============================================================================
section("7. RESOLVE classifier (offline branches)")

yt = resolve.resolve("https://www.youtube.com/watch?v=abc")
check(len(yt) == 1, "youtube single video -> exactly 1 MediaItem")
if yt:
    it = yt[0]
    check(it.kind == "youtube", "youtube video kind == 'youtube'")
    check(it.index == 0, "youtube MediaItem index == 0")
    check(bool(it.name) and bool(it.ref) and bool(it.stem), "youtube MediaItem has name/ref/stem")

direct = resolve.resolve("https://example.com/a.mp4")
check(len(direct) == 1 and direct[0].kind == "direct", "plain http (example.com) -> kind == 'direct'")

# local file path
local_fixture = REPO / "tests" / "_local_fixture.bin"
local_fixture.write_bytes(b"x")
loc = resolve.resolve(str(local_fixture))
check(len(loc) == 1 and loc[0].kind == "file", "local path -> kind == 'file'")
local_fixture.unlink()

# MediaItem field shape
fields = {"index", "name", "kind", "ref", "stem"}
mi = yt[0] if yt else None
check(mi is not None and all(hasattr(mi, f) for f in fields), "MediaItem has index/name/kind/ref/stem")

# resolve.py must carry the yandex discriminator + ported helpers (grep).
resolve_src = (REPO / "core" / "resolve.py").read_text(encoding="utf-8")
check("_embedded" in resolve_src, "resolve.py contains '_embedded' discriminator")
check("def slugify" in resolve_src, "resolve.py contains slugify()")
check("def list_folder" in resolve_src, "resolve.py contains list_folder()")


# ============================================================================
# 8. YADISK REMOVED + knowledge ported
# ============================================================================
section("8. YADISK REMOVED (knowledge ported, not lost)")
check(not (REPO / "yadisk_batch.py").exists(), "yadisk_batch.py no longer exists")
download_src = (REPO / "core" / "download.py").read_text(encoding="utf-8")
check("def list_folder" in resolve_src and "def slugify" in resolve_src, "list_folder+slugify present in core/resolve.py")
check("def download_to" in download_src and "def get_download_url" in download_src, "download_to+get_download_url present in core/download.py")


# ============================================================================
# 9. NO LOGIC LOSS (grep critical flags/branches)
# ============================================================================
section("9. NO LOGIC LOSS")
check('"-ac", "1"' in download_src, "extract_audio keeps '-ac 1' (mono downmix)")
check("libopus" in download_src, "extract_audio keeps libopus")
check('"32k"' in download_src, "extract_audio keeps 32k bitrate")
check("-vn" in download_src, "extract_audio keeps -vn (drop video)")
check("--no-playlist" in download_src, "download_youtube keeps --no-playlist")


# ============================================================================
# 10. SCOPE (Phase-3 boundary: bot.py still untouched). pyproject.toml/uv.lock
#     are intentionally NOT asserted here — Phase 2 legitimately adds
#     fastapi/uvicorn to them. bot.py stays frozen until Phase 3.
# ============================================================================
section("10. SCOPE")
scope_status = subprocess.run(
    ["git", "-C", str(REPO), "status", "--porcelain", "--", "bot.py"],
    capture_output=True, text=True,
)
check(scope_status.stdout.strip() == "",
      f"bot.py NOT modified (Phase 3 boundary; git porcelain empty; got {scope_status.stdout!r})")
check((REPO / "api.py").exists(), "api.py present (Phase 2 deliverable created)")


# ============================================================================
# RESULT
# ============================================================================
print(f"\n{'=' * 60}")
print(f"SUMMARY: {len(_passes)} passed, {len(_failures)} failed")
if _failures:
    print("FAILED CHECKS:")
    for f in _failures:
        print(f"  - {f}")
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
