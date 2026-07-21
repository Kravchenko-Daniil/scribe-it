"""Phase 2 verification — deterministic integration test of api.py with the network mocked.

Independent verifier (NOT the api.py author). No real yt-dlp / requests / ElevenLabs /
network is ever touched: core.resolve.resolve, core.download.extract_audio,
core.scribe.transcribe and core.cache.{key,get,put} are monkeypatched so the on-disk
job model is driven end-to-end through the FastAPI TestClient.

Run: `uv run python tests/check_phase2.py`  (exits non-zero on any failure).

Covers the 10 contract points:
 1. /health 200 only when ELEVENLABS_API_KEY is set (non-200 otherwise; no-key case in a subprocess).
 2. POST precedence/validation (0/>1 source -> 400; local_path outside staging -> 400;
    valid staging local_path -> 200 + uuid hex; GET unknown id -> 404).
 3. Job lifecycle queued->running/done; GET inlines text from items/<index>.txt;
    items/<index>.txt written BEFORE items[i] flips to "done".
 4. status.json / items txt atomicity (os.replace, no leftover .tmp, corrupt read -> graceful).
 5. Folder -> N items; one item failing keeps the job "done", that item "error", rest "done".
 6. Empty resolve -> job error + non-empty error + items=[]; resolve raises -> job error, top-level error.
 7. Cache hit -> scribe.transcribe NOT called, text still rendered.
 8. Semaphore honours SCRIBE_CONCURRENCY (value + max concurrent heavy stages == N).
 9. staging local_path deleted after job (finally); path outside staging NOT deleted.
10. Phase 1 suite still strictly 48 passed / 0 failed / exit 0.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ROLE = os.environ.get("PHASE2_ROLE", "main")


def _mkdirs(base: str) -> None:
    for sub in ("staging", "jobs", "tmp", "cache"):
        pathlib.Path(base, sub).mkdir(parents=True, exist_ok=True)


# --- env MUST be set before importing api (api + core read config at import) -------
if ROLE == "main":
    _BASE = tempfile.mkdtemp(prefix="scribe_phase2_")
    _mkdirs(_BASE)
    os.environ["APP_ENV"] = "test"  # no .env.test exists -> load_dotenv is a no-op
    os.environ["ELEVENLABS_API_KEY"] = "phase2-fake-key"
    os.environ["SCRIBE_STAGING_DIR"] = _BASE + "/staging"
    os.environ["SCRIBE_JOBS_DIR"] = _BASE + "/jobs"
    os.environ["SCRIBE_TMP_DIR"] = _BASE + "/tmp"
    os.environ["SCRIBE_CACHE_DIR"] = _BASE + "/cache"
    os.environ["SCRIBE_CONCURRENCY"] = "2"
# child (health_nokey) receives its env from the parent process (see s1_health).

import api  # noqa: E402
from core.resolve import MediaItem  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# --- health-no-key child: api was imported with NO ELEVENLABS_API_KEY ---------------
if ROLE == "health_nokey":
    with TestClient(api.app) as _c:
        _r = _c.get("/health")
    print(f"[child] /health no-key status={_r.status_code}")
    sys.exit(0 if _r.status_code != 200 else 1)


CONCURRENCY = api.CONCURRENCY
STAGING = api.STAGING_DIR
JOBS = api.JOBS_DIR


# ---------------------------------------------------------------------------------
# Mock control state (mocks read these; reset between scenarios).
# ---------------------------------------------------------------------------------
class MOCK:
    lock = threading.Lock()
    resolve_items: list | None = None
    resolve_empty = False
    resolve_exc: Exception | None = None
    cache_return: dict | None = None       # what cache.get yields (None = miss)
    raise_on_stem: set[str] = set()        # transcribe raises for these opus stems
    block = False                           # transcribe blocks (concurrency probe)
    block_sec = 0.3
    transcribe_calls = 0
    put_calls = 0
    active = 0
    max_active = 0
    ordering_violations: list = []


def _reset(**kw):
    MOCK.resolve_items = None
    MOCK.resolve_empty = False
    MOCK.resolve_exc = None
    MOCK.cache_return = None
    MOCK.raise_on_stem = set()
    MOCK.block = False
    MOCK.transcribe_calls = 0
    MOCK.put_calls = 0
    MOCK.active = 0
    MOCK.max_active = 0
    for k, v in kw.items():
        setattr(MOCK, k, v)


# ---------------------------------------------------------------------------------
# Mocks (replace core functions api holds references to). No network, no disk reads.
# ---------------------------------------------------------------------------------
async def mock_extract_audio(src, out_dir, stem=None):
    # opus path encodes the item stem so transcribe can decide per-item behaviour.
    return pathlib.Path(out_dir) / f"{stem}.opus"


def mock_cache_key(opus_path, language, num_speakers, biased):
    return f"hash_{pathlib.Path(opus_path).stem}_param"


def mock_cache_get(k):
    with MOCK.lock:
        return dict(MOCK.cache_return) if MOCK.cache_return is not None else None


def mock_cache_put(k, data):
    with MOCK.lock:
        MOCK.put_calls += 1


def mock_transcribe(path, api_key, language=None, *, num_speakers=None, biased_keywords=None):
    stem = pathlib.Path(path).stem
    with MOCK.lock:
        MOCK.transcribe_calls += 1
    if MOCK.block:
        with MOCK.lock:
            MOCK.active += 1
            MOCK.max_active = max(MOCK.max_active, MOCK.active)
        time.sleep(MOCK.block_sec)
        with MOCK.lock:
            MOCK.active -= 1
    if stem in MOCK.raise_on_stem:
        raise RuntimeError(f"scribe boom on {stem}")
    return {
        "words": [{"type": "word", "text": "Hi", "start": 0.0, "end": 1.0, "speaker_id": "spk_0"}],
        "text": "Hi",
    }


def mock_resolve(arg):
    if MOCK.resolve_exc is not None:
        raise MOCK.resolve_exc
    if MOCK.resolve_empty:
        return []
    if MOCK.resolve_items is not None:
        return [MediaItem(it.index, it.name, it.kind, it.ref, it.stem) for it in MOCK.resolve_items]
    return [MediaItem(0, "a.mp4", "file", arg, "s0")]


# install mocks onto the exact module objects api holds references to
api.dl.extract_audio = mock_extract_audio
api.cache.key = mock_cache_key
api.cache.get = mock_cache_get
api.cache.put = mock_cache_put
api.scribe.transcribe = mock_transcribe
api.resolve.resolve = mock_resolve

# wrap _flush_status to assert the ordering invariant GLOBALLY: whenever an item is
# flushed as "done", its items/<index>.txt must already exist on disk.
_REAL_FLUSH = api._flush_status


def _wrapped_flush(job_id, state):
    for it in state.get("items", []):
        if it.get("status") == "done":
            p = JOBS / job_id / "items" / f"{it['index']}.txt"
            if not p.exists():
                MOCK.ordering_violations.append((job_id, it.get("index")))
    return _REAL_FLUSH(job_id, state)


api._flush_status = _wrapped_flush


# ---------------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------------
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))


def wait_terminal(client, job_id, timeout=25.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}")
        last = r
        if r.status_code == 200 and r.json().get("status") in ("done", "error"):
            return r.json()
        time.sleep(0.02)
    raise AssertionError(
        f"job {job_id} never terminated; last="
        f"{last.status_code if last else '?'} {(last.text[:200] if last else '')}"
    )


def _mi(index, name, stem, kind="file", ref="x"):
    return MediaItem(index, name, kind, ref, stem)


# ---------------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------------
def s1_health(client):
    r = client.get("/health")
    check("1 health 200 with key", r.status_code == 200,
          f"status={r.status_code} body={r.text[:120]}")

    # no-key: a separate process imports api WITHOUT ELEVENLABS_API_KEY.
    child_base = tempfile.mkdtemp(prefix="scribe_phase2_child_")
    _mkdirs(child_base)
    child_env = dict(os.environ)
    child_env.pop("ELEVENLABS_API_KEY", None)
    child_env["PHASE2_ROLE"] = "health_nokey"
    child_env["APP_ENV"] = "test"
    child_env["SCRIBE_STAGING_DIR"] = child_base + "/staging"
    child_env["SCRIBE_JOBS_DIR"] = child_base + "/jobs"
    child_env["SCRIBE_TMP_DIR"] = child_base + "/tmp"
    child_env["SCRIBE_CACHE_DIR"] = child_base + "/cache"
    cp = subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).resolve())],
        env=child_env, capture_output=True, text=True,
    )
    check("1 health non-200 without key", cp.returncode == 0,
          f"child rc={cp.returncode} out={cp.stdout.strip()[-160:]} err={cp.stderr.strip()[-200:]}")


def s2_post_validation(client):
    r = client.post("/jobs", data={})
    check("2 zero sources -> 400", r.status_code == 400, f"status={r.status_code}")

    inside = STAGING / "dummy_precedence.mp4"
    inside.write_bytes(b"x")
    r = client.post("/jobs", data={"url": "http://e.com/v", "local_path": str(inside)})
    check("2 two sources (url+local_path) -> 400", r.status_code == 400, f"status={r.status_code}")

    r = client.post("/jobs", data={"local_path": "/etc/passwd"})
    check("2 local_path outside staging -> 400", r.status_code == 400, f"status={r.status_code}")

    _reset()
    good = STAGING / "valid_input.mp4"
    good.write_bytes(b"data")
    r = client.post("/jobs", data={"local_path": str(good)})
    ok = r.status_code == 200 and bool(re.fullmatch(r"[0-9a-f]{32}", r.json().get("job_id", "")))
    check("2 valid staging local_path -> 200 + uuid hex", ok,
          f"status={r.status_code} body={r.text[:120]}")
    if r.status_code == 200:
        wait_terminal(client, r.json()["job_id"])

    r = client.get("/jobs/deadbeefdeadbeefdeadbeefdeadbeef")
    check("2 GET unknown id -> 404", r.status_code == 404, f"status={r.status_code}")


def s3_lifecycle(client):
    _reset(resolve_items=[_mi(0, "clip.mp4", "s0")])
    inside = STAGING / "lifecycle.mp4"
    inside.write_bytes(b"data")
    r = client.post("/jobs", data={"local_path": str(inside)})
    check("3 POST accepted (queued returned immediately)", r.status_code == 200, f"status={r.status_code}")
    jid = r.json()["job_id"]

    first = client.get(f"/jobs/{jid}").json()
    check("3 immediate status is a valid job state",
          first["status"] in ("queued", "running", "done"), f"status={first['status']}")

    final = wait_terminal(client, jid)
    check("3 job reaches done", final["status"] == "done", f"status={final['status']}")
    items = final["items"]
    check("3 one item", len(items) == 1, f"len={len(items)}")
    it = items[0]
    check("3 item done", it["status"] == "done", f"status={it['status']}")
    check("3 GET inlines non-empty text", bool(it["text"]), f"text={it['text']!r}")

    disk = JOBS / jid / "items" / f"{it['index']}.txt"
    check("3 items/<index>.txt exists", disk.exists(), str(disk))
    if disk.exists():
        check("3 GET text == file content", it["text"] == disk.read_text(encoding="utf-8"),
              f"get={it['text']!r} disk={disk.read_text(encoding='utf-8')!r}")

    check("3 txt written BEFORE flip to done (no ordering violations)",
          not MOCK.ordering_violations, f"violations={MOCK.ordering_violations}")


def s4_atomicity(client):
    src = (REPO / "api.py").read_text(encoding="utf-8")
    check("4 status.json writer uses os.replace", "os.replace(tmp, path)" in src,
          "missing os.replace in _atomic_write")
    check("4 items txt writer uses os.replace", "os.replace(tmp, p)" in src,
          "missing os.replace in _write_item_txt")
    check("4 reader retries once on broken json", "for attempt in range(2)" in src,
          "no single-retry loop in _read_status")

    _reset(resolve_items=[_mi(0, "atom.mp4", "s0")])
    inside = STAGING / "atom.mp4"
    inside.write_bytes(b"data")
    jid = client.post("/jobs", data={"local_path": str(inside)}).json()["job_id"]
    wait_terminal(client, jid)
    leftovers = list((JOBS / jid).rglob("*.tmp"))
    check("4 no leftover .tmp after job", not leftovers, f"leftovers={leftovers}")

    import json as _json
    valid, detail = True, ""
    try:
        _json.loads((JOBS / jid / "status.json").read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        valid, detail = False, str(e)
    check("4 final status.json is valid JSON", valid, detail)

    # corrupt status.json -> GET is graceful (503), never crashes the app
    cdir = JOBS / "corrupt_job_dir"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "status.json").write_text("{ this is not json", encoding="utf-8")
    r = client.get("/jobs/corrupt_job_dir")
    check("4 corrupt status.json -> graceful 503 (not crash)", r.status_code == 503,
          f"status={r.status_code}")


def s5_folder_n(client):
    items = [_mi(0, "0.mp4", "s0"), _mi(1, "1.mp4", "s1"), _mi(2, "2.mp4", "s2")]
    _reset(resolve_items=items, raise_on_stem={"s1"})
    inside = STAGING / "folder.bin"
    inside.write_bytes(b"data")
    jid = client.post("/jobs", data={"local_path": str(inside)}).json()["job_id"]
    final = wait_terminal(client, jid)
    check("5 job still done despite one failure", final["status"] == "done",
          f"status={final['status']}")
    its = sorted(final["items"], key=lambda x: x["index"])
    check("5 three items", len(its) == 3, f"len={len(its)}")
    check("5 indices 0,1,2", [i["index"] for i in its] == [0, 1, 2],
          f"indices={[i['index'] for i in its]}")
    check("5 item 0 done", its[0]["status"] == "done", f"status={its[0]['status']}")
    check("5 item 1 error", its[1]["status"] == "error", f"status={its[1]['status']}")
    check("5 item 1 has error text", bool(its[1]["error"]), f"error={its[1]['error']}")
    check("5 item 2 done", its[2]["status"] == "done", f"status={its[2]['status']}")
    check("5 failed item text empty", its[1]["text"] == "", f"text={its[1]['text']!r}")
    check("5 done items carry text", bool(its[0]["text"]) and bool(its[2]["text"]),
          f"t0={its[0]['text']!r} t2={its[2]['text']!r}")


def s6_empty_and_broken_resolve(client):
    _reset(resolve_empty=True)
    jid = client.post("/jobs", data={"url": "http://example.com/empty"}).json()["job_id"]
    final = wait_terminal(client, jid)
    check("6 empty resolve -> status error", final["status"] == "error", f"status={final['status']}")
    check("6 empty resolve -> non-empty top-level error", bool(final["error"]),
          f"error={final['error']!r}")
    check("6 empty resolve -> items=[]", final["items"] == [], f"items={final['items']}")

    _reset(resolve_exc=RuntimeError("resolve-blew-up-xyz"))
    jid = client.post("/jobs", data={"url": "http://example.com/bad"}).json()["job_id"]
    final = wait_terminal(client, jid)
    check("6 resolve raises -> status error", final["status"] == "error", f"status={final['status']}")
    check("6 resolve raises -> exception text in top-level error",
          bool(final["error"]) and "resolve-blew-up-xyz" in final["error"], f"error={final['error']!r}")
    check("6 resolve raises -> items=[]", final["items"] == [], f"items={final['items']}")


def s7_cache_hit(client):
    ready = {"words": [{"type": "word", "text": "Cached", "start": 0.0, "end": 1.0, "speaker_id": "spk_0"}]}
    _reset(resolve_items=[_mi(0, "hit.mp4", "s0")], cache_return=ready)
    inside = STAGING / "hit.mp4"
    inside.write_bytes(b"data")
    jid = client.post("/jobs", data={"local_path": str(inside)}).json()["job_id"]
    final = wait_terminal(client, jid)
    check("7 cache-hit job done", final["status"] == "done", f"status={final['status']}")
    check("7 transcribe NOT called on cache hit", MOCK.transcribe_calls == 0,
          f"transcribe_calls={MOCK.transcribe_calls}")
    check("7 cache.put NOT called on cache hit", MOCK.put_calls == 0, f"put_calls={MOCK.put_calls}")
    check("7 text still rendered from cached json", "Cached" in final["items"][0]["text"],
          f"text={final['items'][0]['text']!r}")


def s8_semaphore(client):
    check("8 SEMAPHORE is asyncio.Semaphore", api.SEMAPHORE.__class__.__name__ == "Semaphore",
          f"type={type(api.SEMAPHORE).__name__}")
    check(f"8 SEMAPHORE initial value == SCRIBE_CONCURRENCY ({CONCURRENCY})",
          api.SEMAPHORE._value == CONCURRENCY, f"value={api.SEMAPHORE._value}")

    _reset(block=True, block_sec=0.3)
    MOCK.resolve_items = [_mi(0, "s.mp4", "semstem")]
    ids = []
    for n in range(3):
        inside = STAGING / f"sem_{n}.mp4"
        inside.write_bytes(b"data")
        ids.append(client.post("/jobs", data={"local_path": str(inside)}).json()["job_id"])
    for jid in ids:
        wait_terminal(client, jid, timeout=40.0)
    check("8 all 3 heavy stages ran", MOCK.transcribe_calls == 3,
          f"transcribe_calls={MOCK.transcribe_calls}")
    check(f"8 max concurrent heavy stages <= {CONCURRENCY}", MOCK.max_active <= CONCURRENCY,
          f"max_active={MOCK.max_active}")
    check("8 heavy stages actually overlapped (>=2 concurrent)", MOCK.max_active >= 2,
          f"max_active={MOCK.max_active}")


def s9_staging_cleanup(client):
    _reset(resolve_items=[_mi(0, "clean.mp4", "s0")])
    inside = STAGING / "to_be_deleted.mp4"
    inside.write_bytes(b"data")
    jid = client.post("/jobs", data={"local_path": str(inside)}).json()["job_id"]
    wait_terminal(client, jid)
    gone = False
    for _ in range(150):  # finally runs just after the final flush
        if not inside.exists():
            gone = True
            break
        time.sleep(0.02)
    check("9 staging local_path deleted after job", gone, f"still exists: {inside}")

    outside_dir = tempfile.mkdtemp(prefix="scribe_outside_")
    outside = pathlib.Path(outside_dir, "keepme.mp4")
    outside.write_bytes(b"keep")
    api._safe_delete_staging(str(outside))
    check("9 path outside staging NOT deleted", outside.exists(), f"was deleted: {outside}")

    victim = STAGING / "unit_victim.mp4"
    victim.write_bytes(b"x")
    api._safe_delete_staging(str(victim))
    check("9 staging path deleted by _safe_delete_staging", not victim.exists(), str(victim))


def s10_phase1(_client):
    cp = subprocess.run(
        [sys.executable, str(REPO / "tests" / "check_phase1.py")],
        cwd=str(REPO), capture_output=True, text=True,
    )
    out = cp.stdout + cp.stderr
    ok = cp.returncode == 0 and "48 passed, 0 failed" in out
    tail = "\n".join(out.strip().splitlines()[-3:])
    check("10 phase 1 still 48 passed / 0 failed / exit 0", ok, f"rc={cp.returncode}; {tail}")


# ---------------------------------------------------------------------------------
def main() -> int:
    with TestClient(api.app) as client:
        for fn in (s1_health, s2_post_validation, s3_lifecycle, s4_atomicity,
                   s5_folder_n, s6_empty_and_broken_resolve, s7_cache_hit,
                   s8_semaphore, s9_staging_cleanup, s10_phase1):
            try:
                fn(client)
            except Exception as e:  # noqa: BLE001
                RESULTS.append((f"{fn.__name__} CRASHED", False, repr(e)))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("=" * 70)
    for name, ok, detail in RESULTS:
        line = f"{'PASS' if ok else 'FAIL'}  {name}"
        if not ok and detail:
            line += f"   [{detail}]"
        print(line)
    print("=" * 70)
    print(f"SUMMARY: {passed} passed, {failed} failed")
    print("RESULT:", "PASS" if failed == 0 else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
