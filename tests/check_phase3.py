#!/usr/bin/env python3
"""Deterministic Phase-3 verification — bot.py is now a THIN HTTP client of api.py.

Independent verifier (NOT the author of bot.py). NO real network, NO real Telegram,
NO real aiohttp requests: the aiohttp client session and aiogram Message/Bot objects
are replaced with in-memory fakes, so the bot's client logic is driven end-to-end
against a mocked API.

Run:
    cd /mnt/d/lab/products/scribe-bot && uv run python tests/check_phase3.py

Prints PASS/FAIL per check; exits non-zero if ANY check fails.

Covers docs/PLAN.md Фаза 3 (thin client) contract:
 1. IMPORT CLEAN — `import bot` with fake env, no core imported, no
    downloader/scribe/storage/_archive/ARCHIVE_DIR/SCRIBE_ARCHIVE_DIR, no
    ELEVENLABS_API_KEY read by the bot.
 2. URL_RE — local https?:// sniff present and correct.
 3. _parse_caption still returns the 3-tuple (num, names, language).
 4. PROGRESS render — single-item stage emoji (⬇️/🎧/📝, and ✅ on delivery),
    N-item "готово K/N" counter with correct K.
 5. DELIVERY — one BufferedInputFile per done-item, filename == "<stem>.txt"
    (by STEM, not name), body from item["text"]; error-items → one summary msg.
 6. FORWARD — POST /jobs body carries local_path|url + language + num_speakers
    + speaker_names (verified on a mocked aiohttp session's captured FormData).
 7. STAGING — incoming Telegram file is moved into SCRIBE_STAGING_DIR/<unique>
    via shutil.move; the bot does NOT clean up the staged file.
 8. POLLING resilience — transient GET (raise / 5xx then ok) is RETRIED (no
    abort); >6h ceiling -> error message.
 9. Phases 1-2 suites still green (exit 0).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

# --- fake env MUST be set before importing bot (TOKEN + STAGING read at import) ----
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Format-valid fake token: aiogram validates token FORMAT at module-level Bot()
# construction (bot.py: `bot = _make_bot()`). A literal "dummy" trips aiogram's
# TokenValidationError — that is aiogram's format check on a PRE-EXISTING
# module-level Bot(), unrelated to Phase 3's thin-client refactor (see check 1c,
# which proves the literal-dummy run still reaches that ctor => all imports OK).
FAKE_TOKEN = "123456789:AAFake-Token-For-Import-Test-000000000"
os.environ["TELEGRAM_BOT_TOKEN"] = FAKE_TOKEN
os.environ["APP_ENV"] = "test"  # no .env.test -> load_dotenv is a no-op (deterministic)
os.environ["SCRIBE_API_URL"] = "http://127.0.0.1:8080"
os.environ.pop("ALLOWED_USER_IDS", None)   # empty allow-list -> everyone allowed
os.environ.pop("TG_LOCAL_API_URL", None)

import aiohttp  # noqa: E402

import bot  # noqa: E402  (the module under test)


# ---------------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------------
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


# emoji codepoints the progress UX must carry (stage vocabulary).
EMO_DOWNLOAD = "⬇"      # ⬇️ (base arrow, ignore VS16)
EMO_AUDIO = "\U0001F3A7"     # 🎧
EMO_SCRIBE = "\U0001F4DD"    # 📝
EMO_DONE = "✅"          # ✅


# ---------------------------------------------------------------------------------
# in-memory fakes: aiogram Message/Bot and aiohttp ClientSession
# ---------------------------------------------------------------------------------
class FakeUser:
    def __init__(self, uid=123, username="dan", full_name="Даниил"):
        self.id = uid
        self.username = username
        self.full_name = full_name


class FakeDocument:
    def __init__(self, file_id="F1", file_name="clip.mp4", file_size=1234):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


class FakeStatus:
    def __init__(self):
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        return self

    async def delete(self):
        self.deleted = True


class FakeMessage:
    def __init__(self, from_user=None, caption=None, document=None, text=None, message_id=1):
        self.from_user = from_user or FakeUser()
        self.caption = caption
        self.document = document
        self.video = None
        self.audio = None
        self.voice = None
        self.video_note = None
        self.text = text
        self.message_id = message_id
        self.answers: list[str] = []
        self.documents: list = []
        self._status = FakeStatus()

    async def answer(self, text, **kw):
        self.answers.append(text)
        return self._status

    async def answer_document(self, doc, **kw):
        self.documents.append(doc)
        return self._status


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class _CM:
    """Async context manager mimicking aiohttp's request context."""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self._resp

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, post_resp=None, get_script=None):
        self.post_resp = post_resp or _Resp(200, {"job_id": "jid"})
        self.get_script = list(get_script or [_Resp(200, {})])
        self.get_calls = 0
        self.post_data: list = []

    def post(self, url, data=None):
        self.post_data.append(data)
        return _CM(resp=self.post_resp)

    def get(self, url):
        self.get_calls += 1
        idx = min(self.get_calls - 1, len(self.get_script) - 1)
        item = self.get_script[idx]
        if isinstance(item, Exception):
            return _CM(exc=item)
        return _CM(resp=item)


def _form_fields(fd) -> dict[str, str]:
    """Extract {name: value} from an aiohttp.FormData (internal _fields shape)."""
    out: dict[str, str] = {}
    for type_options, _headers, value in fd._fields:
        out[type_options.get("name")] = value
    return out


# ============================================================================
# 1. IMPORT CLEAN
# ============================================================================
section("1. IMPORT CLEAN (thin client, no core / no ELEVENLABS)")

core_loaded = [m for m in sys.modules if m == "core" or m.startswith("core.")]
check(not core_loaded, "importing bot loads NO core.* module", f"loaded={core_loaded}")

bot_src = (REPO / "bot.py").read_text(encoding="utf-8")
forbidden = re.findall(
    r"import (?:downloader|scribe|storage)|_archive|ARCHIVE_DIR|SCRIBE_ARCHIVE_DIR",
    bot_src,
)
check(not forbidden, "bot.py has NO downloader/scribe/storage import, _archive, ARCHIVE_DIR, SCRIBE_ARCHIVE_DIR",
      f"hits={forbidden}")
check("ELEVENLABS_API_KEY" not in bot_src, "bot.py does NOT read ELEVENLABS_API_KEY (lives in API env)")
check("_periodic_cleanup" not in bot_src and "storage.cleanup" not in bot_src,
      "bot.py has NO _periodic_cleanup / storage.cleanup (cleanups own by API)")

# 1c: literal `dummy` token subprocess — must reach the module-level Bot() ctor
# (proves ALL top-level imports succeeded) and fail ONLY on aiogram's format check,
# NOT on any missing core/downloader/scribe/storage module.
child_env = os.environ.copy()
child_env["TELEGRAM_BOT_TOKEN"] = "dummy"
cp_dummy = subprocess.run(
    [sys.executable, "-c", "import bot; print('BOT_IMPORT_OK')"],
    cwd=str(REPO), env=child_env, capture_output=True, text=True,
)
err = cp_dummy.stdout + cp_dummy.stderr
reached_ctor = "TokenValidationError" in err or "Token is invalid" in err
no_missing_core = not re.search(r"No module named ['\"](?:core|downloader|scribe|storage)", err)
check(reached_ctor and no_missing_core,
      "literal-dummy import fails ONLY at aiogram token-format check (all imports resolved, no missing core/*)",
      f"rc={cp_dummy.returncode} reached_ctor={reached_ctor} no_missing_core={no_missing_core} tail={err.strip()[-160:]}")

# 1d: fresh interpreter, format-valid fake token -> clean import, no core.
cp_ok = subprocess.run(
    [sys.executable, "-c",
     "import sys, bot; print('BOT_IMPORT_OK'); "
     "print('CORE:', [m for m in sys.modules if m=='core' or m.startswith('core.')])"],
    cwd=str(REPO), env=os.environ.copy(), capture_output=True, text=True,
)
out_ok = cp_ok.stdout + cp_ok.stderr
check(cp_ok.returncode == 0 and "BOT_IMPORT_OK" in out_ok and "CORE: []" in out_ok,
      "fresh interpreter imports bot cleanly (BOT_IMPORT_OK, core imports == [])",
      f"rc={cp_ok.returncode} tail={out_ok.strip()[-200:]}")


# ============================================================================
# 2. URL_RE
# ============================================================================
section("2. URL_RE (local https?:// sniff)")
check(hasattr(bot, "URL_RE"), "bot.URL_RE exists (own copy, not imported from core)")
check(bot.URL_RE.match("https://youtu.be/abc") is not None, "URL_RE matches https://…")
check(bot.URL_RE.match("http://disk.yandex.ru/d/x") is not None, "URL_RE matches http://…")
check(bot.URL_RE.match("HTTPS://YouTube.com/x") is not None, "URL_RE case-insensitive (HTTPS://…)")
check(bot.URL_RE.match("just some text") is None, "URL_RE does NOT match plain text")
check(bot.URL_RE.match("ftp://host/x") is None, "URL_RE does NOT match ftp://")


# ============================================================================
# 3. _parse_caption -> (num, names, language)
# ============================================================================
section("3. _parse_caption returns (num, names, language)")
r = bot._parse_caption("2 Даниил Толя")
check(isinstance(r, tuple) and len(r) == 3, "returns a 3-tuple", f"got={r!r}")
check(r == (2, {"speaker_0": "Даниил", "speaker_1": "Толя"}, None),
      "'2 Даниил Толя' -> (2, {speaker_0,speaker_1}, None)", f"got={r!r}")
check(bot._parse_caption("en") == (None, {}, "eng"), "'en' -> (None, {}, 'eng') language only")
check(bot._parse_caption("соло") == (1, {}, None), "'соло' -> (1, {}, None) monologue")
check(bot._parse_caption("2 англ") == (2, {}, "eng"), "'2 англ' -> (2, {}, 'eng') num+language")
check(bot._parse_caption(None) == (None, {}, None), "None caption -> (None, {}, None)")


# ============================================================================
# 4. PROGRESS render
# ============================================================================
section("4. PROGRESS render (stage emoji + K/N counter)")


def _one_item_job(stage):
    return {"status": "running", "items": [
        {"index": 0, "stem": "s", "name": "s.mp4", "text": "", "status": "running",
         "stage": stage, "error": None}]}


def _n_item_job(done_count, total=3):
    items = [
        {"index": i, "stem": f"s{i}", "name": f"{i}.mp4", "text": "",
         "status": "done" if i < done_count else "running", "stage": None, "error": None}
        for i in range(total)
    ]
    return {"status": "running", "items": items}


p_dl = bot._render_progress(_one_item_job("download"))
p_au = bot._render_progress(_one_item_job("audio"))
p_sc = bot._render_progress(_one_item_job("scribe"))
check(EMO_DOWNLOAD in p_dl, "single-item stage 'download' -> ⬇️ emoji", f"got={p_dl!r}")
check(EMO_AUDIO in p_au, "single-item stage 'audio' -> 🎧 emoji", f"got={p_au!r}")
check(EMO_SCRIBE in p_sc, "single-item stage 'scribe' -> 📝 emoji", f"got={p_sc!r}")

n1 = bot._render_progress(_n_item_job(1))
n3 = bot._render_progress(_n_item_job(3))
check("готово 1/3" in n1 and "Обрабатываю 3" in n1,
      "N-item render: 1 done of 3 -> 'Обрабатываю 3 … готово 1/3'", f"got={n1!r}")
check("готово 3/3" in n3, "N-item render: K recomputed each poll (3/3)", f"got={n3!r}")
# K counts only status=="done" (not "running"/"error").
mixed = {"status": "running", "items": [
    {"index": 0, "status": "done", "stage": None},
    {"index": 1, "status": "error", "stage": None},
    {"index": 2, "status": "running", "stage": None},
    {"index": 3, "status": "done", "stage": None},
]}
check("готово 2/4" in bot._render_progress(mixed),
      "K counts only done (2 done, 1 error, 1 running of 4 -> 2/4)",
      f"got={bot._render_progress(mixed)!r}")


# ============================================================================
# 5. DELIVERY (one .txt per done-item by STEM; error summary)
# ============================================================================
section("5. DELIVERY (BufferedInputFile filename == <stem>.txt, body from text)")
deliver_job = {"status": "done", "error": None, "items": [
    {"index": 0, "stem": "первый", "name": "первый.mp4", "text": "AAA текст",
     "status": "done", "stage": None, "error": None},
    {"index": 1, "stem": "второй", "name": "второй.mov", "text": "BBB текст",
     "status": "done", "stage": None, "error": None},
    {"index": 2, "stem": "третий", "name": "третий.mp4", "text": "",
     "status": "error", "stage": None, "error": "boom"},
]}
dmsg = FakeMessage()
dstatus = FakeStatus()
asyncio.run(bot._deliver(dmsg, dstatus, "tag", deliver_job))

check(len(dmsg.documents) == 2, "exactly 2 documents sent (one per done-item)",
      f"n={len(dmsg.documents)}")
if len(dmsg.documents) == 2:
    d0, d1 = dmsg.documents
    from aiogram.types import BufferedInputFile
    check(isinstance(d0, BufferedInputFile) and isinstance(d1, BufferedInputFile),
          "delivered objects are BufferedInputFile (in-memory)")
    check(d0.filename == "первый.txt" and d1.filename == "второй.txt",
          "filenames are <stem>.txt (STEM, not name .mp4/.mov)",
          f"got={d0.filename!r},{d1.filename!r}")
    check(d0.data == "AAA текст".encode("utf-8") and d1.data == "BBB текст".encode("utf-8"),
          "body bytes == item['text'].encode('utf-8')",
          f"d0={d0.data!r}")
check(EMO_DONE in "".join(dstatus.edits),
      "delivery edits status with ✅ (done -> ✅ emoji)", f"edits={dstatus.edits!r}")
check(len(dmsg.answers) == 1 and "третий" in dmsg.answers[0],
      "error-items produce exactly ONE summary message listing the failed item",
      f"answers={dmsg.answers!r}")

# 5b: job-level error (no done items) -> single error message, no documents.
emsg = FakeMessage()
estatus = FakeStatus()
asyncio.run(bot._deliver(emsg, estatus, "tag",
                         {"status": "error", "error": "источник пуст / не распознан", "items": []}))
check(not emsg.documents and any("источник пуст" in e for e in estatus.edits),
      "job status=error -> retransmits terminal error, no documents",
      f"docs={len(emsg.documents)} edits={estatus.edits!r}")


# ============================================================================
# 6. FORWARD (POST body carries source + language + num_speakers + speaker_names)
# ============================================================================
section("6. FORWARD language + num_speakers + speaker_names to POST /jobs")

# 6a: local_path source
sess = FakeSession(post_resp=_Resp(200, {"job_id": "abc123"}))
bot.http = sess
jid = asyncio.run(bot._create_job(
    local_path="/opt/scribe-bot/staging/x/clip.mp4",
    language="eng", num_speakers=2, speaker_names={"speaker_0": "Даниил"},
))
check(jid == "abc123", "_create_job returns job_id from POST response", f"jid={jid!r}")
check(len(sess.post_data) == 1 and isinstance(sess.post_data[0], aiohttp.FormData),
      "POST body is an aiohttp.FormData")
f = _form_fields(sess.post_data[0])
check(f.get("local_path") == "/opt/scribe-bot/staging/x/clip.mp4", "FormData carries local_path", f"f={f}")
check("url" not in f, "local_path job does NOT also send url (exactly one source)", f"f={f}")
check(f.get("language") == "eng", "FormData carries language", f"f={f}")
check(f.get("num_speakers") == "2", "FormData carries num_speakers", f"f={f}")
check("speaker_names" in f and json.loads(f["speaker_names"]) == {"speaker_0": "Даниил"},
      "FormData carries speaker_names as JSON object", f"f={f}")

# 6b: url source
sess2 = FakeSession(post_resp=_Resp(200, {"job_id": "u1"}))
bot.http = sess2
asyncio.run(bot._create_job(url="https://youtu.be/x", language="rus", num_speakers=1, speaker_names={}))
f2 = _form_fields(sess2.post_data[0])
check(f2.get("url") == "https://youtu.be/x" and "local_path" not in f2,
      "url job sends url (and NOT local_path)", f"f2={f2}")
check(f2.get("language") == "rus" and f2.get("num_speakers") == "1",
      "url job forwards language + num_speakers too", f"f2={f2}")


# ============================================================================
# 7. STAGING (shutil.move into SCRIBE_STAGING_DIR/<unique>; no bot cleanup)
# ============================================================================
section("7. STAGING (incoming file -> SCRIBE_STAGING_DIR via shutil.move, no cleanup)")

check("shutil.move" in bot_src, "bot.py uses shutil.move for staging")
check("cleanup(workdir)" not in bot_src, "bot.py has NO finally storage.cleanup(workdir)")

_stage_root = tempfile.mkdtemp(prefix="scribe_ph3_stage_")
_botapi = pathlib.Path(_stage_root, "botapi")
_botapi.mkdir(parents=True, exist_ok=True)
_staging = pathlib.Path(_stage_root, "staging")
_staging.mkdir(parents=True, exist_ok=True)

_SAVED = {
    "STAGING_DIR": bot.STAGING_DIR,
    "LOCAL_API_URL": bot.LOCAL_API_URL,
    "bot": bot.bot,
    "_run_job": bot._run_job,
}
bot.STAGING_DIR = _staging.resolve()
bot.LOCAL_API_URL = "http://localhost:8081"   # force the shutil.move branch in _fetch_to

src_file = _botapi / "telegram_download_source.bin"
src_file.write_bytes(b"VIDEO-BYTES-XYZ")

captured: dict = {}


async def _fake_run_job(msg, status, tag, *, local_path=None, url=None,
                        language=None, num_speakers=None, speaker_names=None):
    captured.update(local_path=local_path, url=url, language=language,
                    num_speakers=num_speakers, speaker_names=speaker_names)


class _FakeFile:
    def __init__(self, fp):
        self.file_path = fp


class _FakeBot:
    async def get_file(self, file_id):
        return _FakeFile(str(src_file))

    async def download_file(self, *a, **k):
        raise AssertionError("bot must NOT HTTP-download in local Bot-API mode (must move)")


bot._run_job = _fake_run_job
bot.bot = _FakeBot()

try:
    mmsg = FakeMessage(
        from_user=FakeUser(),
        caption="2 Даниил Гость",
        document=FakeDocument(file_id="F9", file_name="Интервью с гостем.mp4", file_size=4096),
    )
    asyncio.run(bot.on_media(mmsg))

    check(not src_file.exists(), "source file MOVED out of Bot-API dir (no longer at origin)",
          f"still exists: {src_file}")
    staged = [p for p in _staging.rglob("*") if p.is_file()]
    check(len(staged) == 1, "exactly one staged file under SCRIBE_STAGING_DIR", f"staged={staged}")
    if staged:
        sp = staged[0]
        check(sp.parent.parent == _staging.resolve(),
              "staged into SCRIBE_STAGING_DIR/<unique>/<name> (unique subdir)", f"path={sp}")
        check(sp.name == "Интервью с гостем.mp4", "staged filename keeps human-readable name", f"name={sp.name}")
        check(sp.read_bytes() == b"VIDEO-BYTES-XYZ", "staged bytes intact (real move, not truncated)")
        check(captured.get("local_path") == str(sp.resolve()),
              "bot forwards ABSOLUTE staged path as local_path", f"cap={captured.get('local_path')!r}")
        check(sp.exists(), "bot does NOT delete the staged file (deletion owned by API)")
    check(captured.get("url") is None, "file path -> url is None (local_path source only)")
    check(captured.get("num_speakers") == 2
          and captured.get("speaker_names") == {"speaker_0": "Даниил", "speaker_1": "Гость"},
          "caption hints parsed and forwarded from on_media", f"cap={captured}")
finally:
    bot.STAGING_DIR = _SAVED["STAGING_DIR"]
    bot.LOCAL_API_URL = _SAVED["LOCAL_API_URL"]
    bot.bot = _SAVED["bot"]
    bot._run_job = _SAVED["_run_job"]
    shutil.rmtree(_stage_root, ignore_errors=True)


# ============================================================================
# 8. POLLING resilience (transient GET retried; >6h ceiling -> error msg)
# ============================================================================
section("8. POLLING resilience (transient retry + polling ceiling)")

# 8a: transient GET (ClientError then 200 done) -> retried, then delivers.
_real_sleep = asyncio.sleep


async def _noop_sleep(*a, **k):
    return None


done_payload = {"status": "done", "error": None, "items": [
    {"index": 0, "stem": "clip", "name": "clip.mp4", "text": "hello",
     "status": "done", "stage": None, "error": None}]}
tsess = FakeSession(
    post_resp=_Resp(200, {"job_id": "j1"}),
    get_script=[aiohttp.ClientError("connection refused"), _Resp(200, done_payload)],
)
bot.http = tsess
tmsg = FakeMessage()
tstatus = FakeStatus()
asyncio.sleep = _noop_sleep  # skip real backoff/poll waits
try:
    asyncio.run(bot._run_job(tmsg, tstatus, "tag", url="http://x"))
finally:
    asyncio.sleep = _real_sleep
check(tsess.get_calls >= 2, "transient GET was RETRIED (>=2 GET calls, first failed)",
      f"get_calls={tsess.get_calls}")
check(len(tmsg.documents) == 1 and tmsg.documents[0].filename == "clip.txt",
      "after retry the job completed and delivered clip.txt (no abort)",
      f"docs={[getattr(d,'filename',None) for d in tmsg.documents]}")

# 8b: transient via 5xx status (not just network error) also retried.
tsess2 = FakeSession(
    post_resp=_Resp(200, {"job_id": "j5"}),
    get_script=[_Resp(503, None), _Resp(200, done_payload)],
)
bot.http = tsess2
tmsg2 = FakeMessage()
asyncio.sleep = _noop_sleep
try:
    asyncio.run(bot._run_job(tmsg2, FakeStatus(), "tag", url="http://x"))
finally:
    asyncio.sleep = _real_sleep
check(tsess2.get_calls >= 2 and len(tmsg2.documents) == 1,
      "5xx GET is transient too (retried, then delivered)",
      f"get_calls={tsess2.get_calls} docs={len(tmsg2.documents)}")

# 8c: polling ceiling (>6h) -> error message, no infinite loop.
_real_ceiling = bot.POLL_CEILING_SEC
bot.POLL_CEILING_SEC = -1  # deadline already in the past -> first loop iteration aborts
running_payload = {"status": "running", "error": None, "items": [
    {"index": 0, "stem": "x", "name": "x", "text": "", "status": "running",
     "stage": "download", "error": None}]}
csess = FakeSession(post_resp=_Resp(200, {"job_id": "jc"}), get_script=[_Resp(200, running_payload)])
bot.http = csess
cmsg = FakeMessage()
cstatus = FakeStatus()
try:
    asyncio.run(bot._run_job(cmsg, cstatus, "tag", url="http://x"))
finally:
    bot.POLL_CEILING_SEC = _real_ceiling
ceiling_msg = "".join(cstatus.edits)
check("6 час" in ceiling_msg or "не завершилась" in ceiling_msg,
      "exceeding polling ceiling -> user-facing timeout/error message",
      f"edits={cstatus.edits!r}")
check(not cmsg.documents, "ceiling abort delivers no transcript documents")

bot.http = None  # tidy


# ============================================================================
# 9. Phases 1-2 still green
# ============================================================================
section("9. Phases 1-2 suites still green (exit 0)")
for name in ("check_phase1.py", "check_phase2.py"):
    cp = subprocess.run(
        [sys.executable, str(REPO / "tests" / name)],
        cwd=str(REPO), env=os.environ.copy(), capture_output=True, text=True,
    )
    out = cp.stdout + cp.stderr
    tail = "\n".join(out.strip().splitlines()[-2:])
    check(cp.returncode == 0 and "0 failed" in out,
          f"{name} PASS (exit 0, 0 failed)", f"rc={cp.returncode}; {tail}")


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
