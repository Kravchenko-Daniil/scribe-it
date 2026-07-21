"""scribe-it HTTP API — job-модель на диске поверх core/.

Один сервис: POST /jobs, GET /jobs/{id}, GET /health. Ядро (core/) блокирующее —
всё тяжёлое (resolve, download, ffmpeg, sha256, Scribe HTTP, рендер) гоняется через
asyncio.to_thread. Джоба = фоновая asyncio-задача; её status.json — единственный
писатель сама задача, каждая запись атомарна (.tmp + os.replace). Хранилище на диске
переживает рестарт: незавершённые джобы возобновляются на старте (lifespan).

Границы Фазы 2: бот не трогаем. Env читается как в bot.py (load_dotenv по APP_ENV).
Ключ Scribe — ELEVENLABS_API_KEY (живёт в env API, не бота).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import shutil
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

# --- env: как в bot.py, load_dotenv по APP_ENV, ДО импорта core -----------------
# core.storage/cache/download читают SCRIBE_TMP_DIR/SCRIBE_CACHE_DIR/COOKIES_PATH
# на импорте — поэтому .env должен быть загружен раньше этих импортов.
APP_ENV = os.environ.get("APP_ENV", "local")
load_dotenv(pathlib.Path(__file__).resolve().parent / f".env.{APP_ENV}")

import core.cache as cache  # noqa: E402
import core.download as dl  # noqa: E402
import core.resolve as resolve  # noqa: E402
import core.scribe as scribe  # noqa: E402
import core.storage as storage  # noqa: E402

# --- фиксированные константы (env-с-дефолтом, абсолютные пути) -------------------
REPO_ROOT = pathlib.Path(__file__).resolve().parent

STAGING_DIR = pathlib.Path(
    os.environ.get("SCRIBE_STAGING_DIR") or (REPO_ROOT / "staging")
).resolve()
JOBS_DIR = pathlib.Path(
    os.environ.get("SCRIBE_JOBS_DIR") or (REPO_ROOT / "jobs")
).resolve()

API_HOST = os.environ.get("SCRIBE_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("SCRIBE_API_PORT", "8080"))
CONCURRENCY = int(os.environ.get("SCRIBE_CONCURRENCY", "2"))

# staging-janitor и tmp-cleanup — разные механизмы; дефолт janitor = 6ч (как
# cleanup_old в старом боте), tmp держим дольше (workdir может жить под 6-часовой
# джобой — Scribe-таймаут 6ч), чтобы cleanup_old не снёс активный workdir.
STAGING_TTL_HOURS = float(os.environ.get("STAGING_TTL_HOURS", "6"))
TMP_TTL_HOURS = float(os.environ.get("SCRIBE_TMP_TTL_HOURS", "24"))
CLEANUP_INTERVAL_SEC = float(os.environ.get("SCRIBE_CLEANUP_INTERVAL_SEC", "3600"))

# Ключ Scribe живёт в env API. НЕ падаем на импорте при его отсутствии — /health
# должен подняться и вернуть не-200, чтобы охранить прод-катовер.
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("scribe-api")

# Глобальный семафор гейтит тяжёлую per-item стадию (download+ffmpeg+Scribe) кросс-
# джобно: суммарно in-flight Scribe <= CONCURRENCY. Элементы одной джобы идут
# последовательно (см. run_job — цикл по items с await). В 3.10+ примитив лениво
# привязывается к running loop, конструирование на импорте безопасно.
SEMAPHORE = asyncio.Semaphore(CONCURRENCY)

# Держим ссылки на фоновые задачи джоб, чтобы их не собрал GC.
_TASKS: set[asyncio.Task] = set()


# --- on-disk job store ----------------------------------------------------------

def _job_dir(job_id: str) -> pathlib.Path:
    return JOBS_DIR / job_id


def _atomic_write(path: pathlib.Path, text: str) -> None:
    """Атомарная запись текста: <name>.tmp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _flush_status(job_id: str, state: dict) -> None:
    """Единственный писатель status.json — вызывающая job-task. Атомарно."""
    _atomic_write(_job_dir(job_id) / "status.json", json.dumps(state, ensure_ascii=False))


def _read_status(job_id: str) -> dict | None:
    """Прочитать status.json c ОДНИМ ретраем на битом JSON (гонка с os.replace).

    None → status.json нет. При неустранимой порче после ретрая — HTTP 503
    (транзиентно; бот ретраит GET с backoff).
    """
    p = _job_dir(job_id) / "status.json"
    for attempt in range(2):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError):
            if attempt == 0:
                time.sleep(0.05)
                continue
    raise HTTPException(status_code=503, detail="status temporarily unavailable")


def _read_status_quiet(job_id: str) -> dict | None:
    """Прочитать status.json без ретрая/исключений — для внутренних нужд (resume)."""
    p = _job_dir(job_id) / "status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_job_meta(job_id: str) -> dict | None:
    p = _job_dir(job_id) / "job.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_item_txt(job_id: str, index: int, text: str) -> None:
    """Атомарная запись items/<index>.txt (по index, не по позиции)."""
    items_dir = _job_dir(job_id) / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    p = items_dir / f"{index}.txt"
    tmp = items_dir / f"{index}.txt.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _read_item_txt(job_id: str, index: int) -> str:
    p = _job_dir(job_id) / "items" / f"{index}.txt"
    try:
        return p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _safe_delete_staging(path_str: str) -> None:
    """Удалить local_path после джобы — ТОЛЬКО если он под SCRIBE_STAGING_DIR."""
    try:
        p = pathlib.Path(path_str).resolve()
        if not p.is_relative_to(STAGING_DIR) or not p.exists():
            return
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()
    except Exception:
        log.exception("failed to delete staging path %s", path_str)


# --- per-item pipeline ----------------------------------------------------------

async def _download(item: resolve.MediaItem, wd: pathlib.Path) -> pathlib.Path:
    """Скачать элемент по kind в его собственный workdir. Возвращает src (до ffmpeg)."""
    if item.kind == "youtube":
        return await dl.download_youtube(item.ref, wd)
    if item.kind == "yandex":
        obj = json.loads(item.ref)  # сериализованный (public_key, path)
        href = await asyncio.to_thread(dl.get_download_url, obj["public_key"], obj["path"])
        suffix = pathlib.Path(item.name).suffix
        dst = wd / f"source{suffix}"
        wd.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(dl.download_to, href, dst)
        return dst
    if item.kind == "direct":
        return await dl.download_direct(item.ref, wd)
    # kind == "file" — уже на диске (staging / staged upload)
    return pathlib.Path(item.ref)


async def _process_item(
    job_id: str,
    state: dict,
    pos: int,
    item: resolve.MediaItem,
    *,
    language: str | None,
    num_speakers: int | None,
    biased: list[str] | None,
    speaker_names: dict[str, str],
) -> None:
    """Один элемент: download → extract_audio(opus 32k mono) → cache/Scribe → рендер.

    Пер-элементный сбой локализуется: items[pos].status="error", джоба продолжает.
    Тяжёлое ядро — через to_thread. stage пишется ПЕРЕД каждой стадией.
    """
    entry = state["items"][pos]
    wd: pathlib.Path | None = None
    try:
        entry["status"] = "running"
        entry["stage"] = "download"
        _flush_status(job_id, state)

        wd = await asyncio.to_thread(storage.new_workdir)  # свой workdir на элемент
        src = await _download(item, wd)

        entry["stage"] = "audio"
        _flush_status(job_id, state)
        # Все kind терминируются в extract_audio → opus 32k mono (моно-даунмикс
        # обязателен: Scribe теряет текст на стерео). Это единственный хешируемый выход.
        opus = await dl.extract_audio(src, wd, stem=item.stem)

        entry["stage"] = "scribe"
        _flush_status(job_id, state)
        # sha256(opus) — CPU-bound на больших файлах → to_thread. cache.key сортирует
        # biased внутри, ключ порядко-независим. opus_hash = первая половина ключа.
        cache_key = await asyncio.to_thread(cache.key, opus, language, num_speakers, biased)
        entry["opus_hash"] = cache_key.split("_", 1)[0]

        data = await asyncio.to_thread(cache.get, cache_key)
        if data is None:  # промах → платим Scribe → кладём в кеш
            data = await asyncio.to_thread(
                scribe.transcribe,
                opus,
                ELEVENLABS_API_KEY,
                language,
                num_speakers=num_speakers,
                biased_keywords=biased,
            )
            await asyncio.to_thread(cache.put, cache_key, data)
        # попадание → Scribe пропущен (без оплаты)

        words = [w for w in data.get("words", []) if w.get("type") == "word"]
        if words:
            text = await asyncio.to_thread(
                scribe.to_paragraphs, words, speaker_names=speaker_names or None
            )
        else:
            text = (data.get("text", "").strip() + "\n")

        # items/<index>.txt пишется атомарно ДО флипа items[pos].status в "done".
        await asyncio.to_thread(_write_item_txt, job_id, item.index, text)
        entry["status"] = "done"
        entry["stage"] = None
        entry["error"] = None
        _flush_status(job_id, state)
    except Exception as e:  # пер-элементный сбой — не валит джобу
        log.exception("job %s item %s failed", job_id, item.index)
        entry["status"] = "error"
        entry["stage"] = None
        entry["error"] = str(e)
        _flush_status(job_id, state)
    finally:
        if wd is not None:
            await asyncio.to_thread(storage.cleanup, wd)


async def run_job(job_id: str) -> None:
    """Фоновая задача джобы. resolve → per-item pipeline → терминальный статус.

    Единственный писатель status.json этой джобы. Идемпотентна к возобновлению:
    уже готовые (done + items/<index>.txt на диске) элементы не переобрабатываются.
    """
    meta = _read_job_meta(job_id)
    if meta is None:
        _flush_status(
            job_id,
            {"status": "error", "error": "job metadata missing", "items": []},
        )
        return

    resolve_arg: str = meta["resolve_arg"]
    cleanup_path: str | None = meta.get("cleanup_local_path")
    language: str | None = meta.get("language")
    num_speakers: int | None = meta.get("num_speakers")
    speaker_names: dict[str, str] = meta.get("speaker_names") or {}
    biased = list(speaker_names.values()) or None

    try:
        # resolve — внутри задачи (не на приёме POST). Синхронный → to_thread.
        try:
            items = await asyncio.to_thread(resolve.resolve, resolve_arg)
        except Exception as e:
            log.exception("job %s resolve failed", job_id)
            _flush_status(job_id, {"status": "error", "error": str(e), "items": []})
            return

        if not items:  # пустой список → терминальный error (иначе бот поллит вечно)
            _flush_status(
                job_id,
                {"status": "error", "error": "источник пуст / не распознан", "items": []},
            )
            return

        # Сверка с уже готовыми элементами (возобновление после рестарта).
        prior = _read_status_quiet(job_id) or {}
        prior_map = {i.get("index"): i for i in prior.get("items", [])}
        state: dict = {"status": "running", "error": None, "items": []}
        for it in items:
            prev = prior_map.get(it.index)
            done_on_disk = (_job_dir(job_id) / "items" / f"{it.index}.txt").exists()
            if prev and prev.get("status") == "done" and done_on_disk:
                state["items"].append({
                    "index": it.index, "name": it.name, "stem": it.stem,
                    "status": "done", "stage": None, "error": None,
                    "opus_hash": prev.get("opus_hash"),
                })
            else:
                state["items"].append({
                    "index": it.index, "name": it.name, "stem": it.stem,
                    "status": "queued", "stage": None, "error": None, "opus_hash": None,
                })
        _flush_status(job_id, state)

        # Элементы одной джобы — последовательно; семафор ограничивает кросс-джобную
        # параллельность тяжёлой стадии.
        for pos, it in enumerate(items):
            if state["items"][pos]["status"] == "done":
                continue
            async with SEMAPHORE:
                await _process_item(
                    job_id, state, pos, it,
                    language=language, num_speakers=num_speakers,
                    biased=biased, speaker_names=speaker_names,
                )

        any_done = any(i["status"] == "done" for i in state["items"])
        state["status"] = "done" if any_done else "error"
        if not any_done:
            state["error"] = "не удалось обработать ни один элемент"
        _flush_status(job_id, state)
    finally:
        # Удаляем переданный local_path после джобы — только если он под staging.
        if cleanup_path:
            _safe_delete_staging(cleanup_path)


# --- cleanups (владеет API) -----------------------------------------------------

def _staging_janitor() -> int:
    """Sweep SCRIBE_STAGING_DIR: удалить осиротевшие (бот упал между move и POST)
    старше STAGING_TTL_HOURS."""
    if not STAGING_DIR.exists():
        return 0
    cutoff = time.time() - STAGING_TTL_HOURS * 3600
    removed = 0
    for p in STAGING_DIR.iterdir():
        try:
            if p.stat().st_mtime >= cutoff:
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("staging janitor failed on %s", p)
    return removed


async def _cleanup_loop() -> None:
    while True:
        try:
            tmp_removed = await asyncio.to_thread(storage.cleanup_old, TMP_TTL_HOURS)
            evicted = await asyncio.to_thread(cache.evict)
            staged = await asyncio.to_thread(_staging_janitor)
            if tmp_removed or evicted or staged:
                log.info(
                    "cleanup: tmp=%d cache=%d staging=%d",
                    tmp_removed, evicted, staged,
                )
        except Exception:
            log.exception("cleanup loop error")
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)


async def _resume_jobs() -> None:
    """Возобновить незавершённые джобы после рестарта (queued/running)."""
    if not JOBS_DIR.exists():
        return
    for jd in sorted(JOBS_DIR.iterdir()):
        if not jd.is_dir():
            continue
        st = _read_status_quiet(jd.name)
        if st is None:
            continue
        if st.get("status") in ("queued", "running"):
            log.info("resuming job %s (was %s)", jd.name, st.get("status"))
            _spawn(run_job(jd.name))


@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    if not ELEVENLABS_API_KEY:
        log.warning("ELEVENLABS_API_KEY not set — /health will report non-200")
    await _resume_jobs()
    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title="scribe-it API", lifespan=lifespan)


# --- endpoints ------------------------------------------------------------------

@app.post("/jobs")
async def create_job(
    local_path: str | None = Form(default=None),
    url: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    language: str | None = Form(default=None),
    num_speakers: int | None = Form(default=None),
    speaker_names: str | None = Form(default=None),
) -> dict:
    """Принять источник, вернуть {job_id} и статус queued мгновенно.

    Ровно один источник из {local_path | multipart-файл | url}. Precedence
    local_path > multipart > url; 0 или >1 источника → 400. resolve НЕ на приёме —
    внутри job-task. speaker_names — JSON-объект (speaker_id → display-name).
    """
    local_path = (local_path or "").strip() or None
    url = (url or "").strip() or None
    has_file = file is not None and bool(file.filename)

    if sum([bool(local_path), bool(has_file), bool(url)]) != 1:
        raise HTTPException(
            status_code=400,
            detail="provide exactly one source: local_path | file | url",
        )

    names: dict[str, str] = {}
    if speaker_names:
        try:
            parsed = json.loads(speaker_names)
            if not isinstance(parsed, dict):
                raise ValueError
            names = {str(k): str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="speaker_names must be a JSON object")

    cleanup_path: str | None = None
    if local_path:  # precedence 1 — быстрый путь одного хоста, основной для бота
        p = pathlib.Path(local_path).resolve()
        if not p.is_relative_to(STAGING_DIR):
            raise HTTPException(status_code=400, detail="local_path must be under staging dir")
        resolve_arg = str(p)
        cleanup_path = str(p)  # local_path под staging → удаляем в finally джобы
    elif has_file:  # precedence 2 — multipart стейджится в API-workdir (чистит cleanup_old)
        wd = await asyncio.to_thread(storage.new_workdir)
        dst = wd / (pathlib.Path(file.filename).name or "upload")
        with dst.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
        resolve_arg = str(dst)
    else:  # precedence 3 — url
        resolve_arg = url

    job_id = uuid.uuid4().hex
    meta = {
        "resolve_arg": resolve_arg,
        "cleanup_local_path": cleanup_path,
        "language": language,
        "num_speakers": num_speakers,
        "speaker_names": names,
    }
    (_job_dir(job_id) / "items").mkdir(parents=True, exist_ok=True)
    _atomic_write(_job_dir(job_id) / "job.json", json.dumps(meta, ensure_ascii=False))
    _flush_status(job_id, {"status": "queued", "error": None, "items": []})
    _spawn(run_job(job_id))
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    """Статус джобы + элементы. text инлайнится из items/<index>.txt для done, "" для error."""
    if not _job_dir(job_id).is_dir():
        raise HTTPException(status_code=404, detail="job not found")
    st = _read_status(job_id)  # ОДИН ретрай на битом JSON внутри
    if st is None:
        raise HTTPException(status_code=404, detail="job not found")

    items_out = []
    for it in st.get("items", []):
        text = _read_item_txt(job_id, it["index"]) if it.get("status") == "done" else ""
        items_out.append({
            "index": it["index"],
            "name": it["name"],
            "stem": it.get("stem", ""),  # additive: боту нужен для имени <stem>.txt
            "text": text,
            "status": it["status"],
            "stage": it.get("stage"),
            "error": it.get("error"),
        })
    return {"status": st["status"], "items": items_out, "error": st.get("error")}


@app.get("/health")
def health():
    """200 ТОЛЬКО если API прочитал ELEVENLABS_API_KEY (охраняет прод-катовер)."""
    if not ELEVENLABS_API_KEY:
        return JSONResponse(
            {"status": "error", "reason": "ELEVENLABS_API_KEY not set"},
            status_code=503,
        )
    return {"status": "ok", "cookies_present": dl.COOKIES_PATH.exists()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=API_HOST, port=API_PORT)
