#!/usr/bin/env python3
"""Deterministic verification of cookie-staleness detection (core.download.read_cookie_status)
and its exposure through GET /health.

Plain-python (no pytest), same shape as tests/check_phase*.py. Run:
    cd /mnt/d/lab/products/scribe-bot && uv run python tests/check_cookie_status.py

Prints PASS/FAIL per check; exits non-zero if ANY check fails.

Problem this covers: cookies.txt can exist on disk while the YouTube session
inside it is dead (expired). The old /health only reported "file present",
which stayed true long after yt-dlp started failing with "Sign in to confirm
you're not a bot". read_cookie_status() parses the Netscape cookie file and
reports the nearest session-cookie expiry, all filesystem-only (no network).

Covers:
 1. Missing cookies.txt -> present=False, expired=True, no ages/timestamps.
 2. Live cookies -> present=True, expired=False, expires_at == the NEAREST
    (soonest) expiry among session cookies, non-session cookies ignored.
 3. Expired cookies -> present=True, expired=True, expires_at in the past.
 4. Malformed lines (too few fields, non-integer expiry, blank, comment) are
    skipped without raising; a valid line elsewhere in the same file still
    parses. A file with ONLY malformed/foreign lines -> expired=True (no
    usable session cookie found), not a crash.
 5. GET /health stays cheap (no network) and extends compatibly: the old
    cookies_present field keeps its old meaning while the new cookies.* block
    surfaces the expired session even though the file is still "present".
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import core.download as dl  # noqa: E402

_failures: list[str] = []
_passes: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    if cond:
        print(f"PASS  {label}")
        _passes.append(label)
    else:
        line = f"FAIL  {label}"
        if detail:
            line += f"   [{detail}]"
        print(line)
        _failures.append(f"{label} :: {detail}" if detail else label)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


_TMP = pathlib.Path(tempfile.mkdtemp(prefix="scribe_cookie_status_"))


def _write_cookies(name: str, lines: list[str]) -> pathlib.Path:
    p = _TMP / name
    p.write_text(
        "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n", encoding="utf-8"
    )
    return p


def _line(name: str, expiry: int | str, domain: str = ".youtube.com") -> str:
    return f"{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\tval-{name}"


NOW = int(time.time())
FUTURE_SOON = NOW + 3600          # 1h from now
FUTURE_LATER = NOW + 7 * 24 * 3600  # 1 week from now
PAST = NOW - 3600                 # expired 1h ago


# ============================================================================
# 1. MISSING FILE
# ============================================================================
section("1. MISSING cookies.txt")
missing = _TMP / "does-not-exist.txt"
st = dl.read_cookie_status(missing)
check(st.present is False, "missing file -> present=False", f"got={st}")
check(st.expired is True, "missing file -> expired=True", f"got={st}")
check(st.age_seconds is None, "missing file -> age_seconds=None", f"got={st}")
check(st.expires_at is None, "missing file -> expires_at=None", f"got={st}")


# ============================================================================
# 2. LIVE COOKIES — nearest expiry among session cookies, foreign cookies ignored
# ============================================================================
section("2. LIVE cookies (session cookies not yet expired)")
live = _write_cookies("live.txt", [
    _line("SID", FUTURE_LATER),
    _line("HSID", FUTURE_SOON),        # earlier of the two -> should win as "nearest"
    _line("__Secure-3PSID", FUTURE_LATER),
    _line("CONSENT", PAST),            # non-session cookie, already expired -> must be ignored
])
st = dl.read_cookie_status(live)
check(st.present is True, "live file -> present=True", f"got={st}")
check(st.expired is False, "live file -> expired=False (nearest session cookie still valid)", f"got={st}")
check(st.expires_at == FUTURE_SOON,
      "live file -> expires_at is the NEAREST (soonest) session-cookie expiry, ignoring CONSENT",
      f"got={st.expires_at} want={FUTURE_SOON}")
check(st.age_seconds is not None and 0 <= st.age_seconds < 30,
      "live file -> age_seconds reflects just-written mtime", f"got={st.age_seconds}")


# ============================================================================
# 3. EXPIRED COOKIES
# ============================================================================
section("3. EXPIRED cookies (session cookie already past expiry)")
expired = _write_cookies("expired.txt", [
    _line("SID", PAST),
    _line("SAPISID", PAST - 100),
])
st = dl.read_cookie_status(expired)
check(st.present is True, "expired file -> present=True (file still exists on disk)", f"got={st}")
check(st.expired is True, "expired file -> expired=True", f"got={st}")
check(st.expires_at == PAST - 100,
      "expired file -> expires_at is the NEAREST/soonest expiry among session cookies",
      f"got={st.expires_at} want={PAST - 100}")


# ============================================================================
# 4. MALFORMED LINES — skipped, not fatal
# ============================================================================
section("4. MALFORMED lines (skipped without raising)")
malformed = _write_cookies("malformed.txt", [
    "",                                  # blank
    "# a comment line",                  # comment
    "too\tfew\tfields",                  # < 7 fields
    f".youtube.com\tTRUE\t/\tTRUE\tnot-a-number\tSSID\tval",  # non-integer expiry
    _line("SID", FUTURE_SOON),           # one valid line among the noise
])
try:
    st = dl.read_cookie_status(malformed)
    raised = False
except Exception as exc:  # noqa: BLE001 — proving NO exception escapes, whatever it is
    raised = True
    st = None
    exc_repr = repr(exc)
check(not raised, "malformed lines do not raise", "" if not raised else exc_repr)
if not raised:
    check(st.expired is False, "malformed lines skipped, the one valid SID line still parsed",
          f"got={st}")
    check(st.expires_at == FUTURE_SOON, "valid line's expiry used despite surrounding noise",
          f"got={st.expires_at} want={FUTURE_SOON}")

section("4b. ONLY malformed/foreign lines -> no usable session cookie -> expired")
only_noise = _write_cookies("only_noise.txt", [
    "garbage line with no tabs",
    _line("CONSENT", FUTURE_LATER),      # present, but not a session cookie
])
st = dl.read_cookie_status(only_noise)
check(st.present is True, "file with only noise -> present=True (file exists)", f"got={st}")
check(st.expired is True, "file with only noise -> expired=True (no session cookie found)", f"got={st}")
check(st.expires_at is None, "file with only noise -> expires_at=None", f"got={st}")


# ============================================================================
# 5. GET /health — additive, backward-compatible, no network
# ============================================================================
section("5. /health stays cheap and extends compatibly")


def _health_response(cookies_path: pathlib.Path) -> dict:
    """Import api.py fresh in a subprocess with COOKIES_PATH pointed at cookies_path,
    hit /health via TestClient, return the parsed JSON body. Subprocess isolation avoids
    reloading api.py's module-level state in this test process."""
    base = pathlib.Path(tempfile.mkdtemp(prefix="scribe_cookie_health_"))
    for sub in ("staging", "jobs", "tmp", "cache"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "APP_ENV": "test",
        "ELEVENLABS_API_KEY": "cookie-status-test-key",
        "COOKIES_PATH": str(cookies_path),
        "SCRIBE_STAGING_DIR": str(base / "staging"),
        "SCRIBE_JOBS_DIR": str(base / "jobs"),
        "SCRIBE_TMP_DIR": str(base / "tmp"),
        "SCRIBE_CACHE_DIR": str(base / "cache"),
    })
    script = (
        "import sys, json; sys.path.insert(0, sys.argv[1]); "
        "import api; from fastapi.testclient import TestClient; "
        "c = TestClient(api.app); print(json.dumps(c.get('/health').json()))"
    )
    cp = subprocess.run(
        [sys.executable, "-c", script, str(REPO)],
        env=env, capture_output=True, text=True,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"health child failed rc={cp.returncode}\n{cp.stdout}\n{cp.stderr}")
    return json.loads(cp.stdout.strip().splitlines()[-1])


body_live = _health_response(live)
check(body_live.get("status") == "ok", "live cookies -> status:ok unchanged", f"body={body_live}")
check(body_live.get("cookies_present") is True,
      "live cookies -> old field cookies_present:true kept as-is", f"body={body_live}")
check(isinstance(body_live.get("cookies"), dict) and body_live["cookies"].get("expired") is False,
      "live cookies -> new cookies.expired:false", f"body={body_live}")

body_expired = _health_response(expired)
check(body_expired.get("cookies_present") is True,
      "expired cookies -> cookies_present STILL true (file exists — old blind spot)",
      f"body={body_expired}")
check(body_expired.get("cookies", {}).get("expired") is True,
      "expired cookies -> new cookies.expired:true makes the blind spot visible",
      f"body={body_expired}")
check(body_expired.get("cookies", {}).get("expires_at") == PAST - 100,
      "expired cookies -> cookies.expires_at reported to the caller", f"body={body_expired}")

body_missing = _health_response(missing)
check(body_missing.get("cookies_present") is False, "missing cookies -> cookies_present:false",
      f"body={body_missing}")
check(body_missing.get("cookies", {}).get("expired") is True,
      "missing cookies -> cookies.expired:true", f"body={body_missing}")


# ============================================================================
# RESULT
# ============================================================================
print(f"\n{'=' * 60}")
print(f"SUMMARY: {len(_passes)} passed, {len(_failures)} failed")
if _failures:
    print("FAILED CHECKS:")
    for fl in _failures:
        print(f"  - {fl}")
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
