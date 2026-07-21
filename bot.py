"""Telegram bot — тонкий HTTP-клиент scribe-it API.

Вся работа (resolve → download → ffmpeg → Scribe → рендер) живёт в API (api.py).
Бот только: принимает файл/ссылку из Telegram, стейджит файл на общую ФС, зовёт
`POST /jobs`, поллит `GET /jobs/{id}` и отдаёт готовый `.txt` по каждому элементу.

Ключ ElevenLabs здесь НЕ читается — он живёт в env API. Файлы передаются в API
по локальному пути (`local_path`) под общим `SCRIBE_STAGING_DIR` (та же ФС, что и
download-каталог локального Bot-API) — без повторной перезаливки.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import pathlib
import re
import shutil
import time
import uuid

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from dotenv import load_dotenv

APP_ENV = os.environ.get("APP_ENV", "local")
load_dotenv(pathlib.Path(__file__).parent / f".env.{APP_ENV}")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED: set[int] = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
}
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or "0")
LOCAL_API_URL = os.environ.get("TG_LOCAL_API_URL", "").strip() or None
TG_FILE_LIMIT = 2 * 1024 * 1024 * 1024 if LOCAL_API_URL else 20 * 1024 * 1024
TG_FILE_LIMIT_LABEL = "2 ГБ" if LOCAL_API_URL else "20 МБ"

# API-клиент. Тот же .env, что у api.py → SCRIBE_STAGING_DIR совпадает у обоих.
SCRIBE_API_URL = (os.environ.get("SCRIBE_API_URL") or "http://127.0.0.1:8080").rstrip("/")
STAGING_DIR = pathlib.Path(
    os.environ.get("SCRIBE_STAGING_DIR") or (pathlib.Path(__file__).resolve().parent / "staging")
).resolve()
# Download-каталог локального Bot-API — источник файлов для shutil.move в staging.
# Должен быть на ТОЙ ЖЕ ФС, что STAGING_DIR (иначе move деградирует в copy 2 ГБ).
# Дефолт = штатный --dir сервера telegram-bot-api (на проде он запущен с
# --dir=/var/lib/telegram-bot-api); переопределяется env BOTAPI_DOWNLOAD_DIR.
BOTAPI_DOWNLOAD_DIR = pathlib.Path(
    os.environ.get("BOTAPI_DOWNLOAD_DIR") or "/var/lib/telegram-bot-api"
)

# Поллинг джобы.
POLL_INTERVAL_SEC = 5
POLL_CEILING_SEC = 6 * 3600 + 600  # > 6ч (таймаут Scribe) + запас
BACKOFF_START_SEC = 2
BACKOFF_MAX_SEC = 60

MEDIA_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v",
    ".3gp", ".ts", ".mts", ".mpeg", ".mpg", ".ogv",
    ".opus", ".ogg", ".oga", ".mp3", ".wav", ".flac", ".aac", ".m4a", ".wma",
}

SOLO_WORDS = {"соло", "solo", "монолог", "monolog", "monologue"}
DUO_WORDS = {"диалог", "dialog", "dialogue", "созвон", "call"}

# Optional language override in caption. Maps keyword → ElevenLabs language_code.
# Without a hint the caption omits language and Scribe auto-detects.
LANG_WORDS = {
    "en": "eng", "eng": "eng", "англ": "eng", "english": "eng",
    "ru": "rus", "rus": "rus", "рус": "rus", "russian": "rus",
    "uk": "ukr", "ukr": "ukr", "укр": "ukr",
    "de": "deu", "deu": "deu", "нем": "deu",
    "fr": "fra", "fra": "fra", "фр": "fra",
    "es": "spa", "spa": "spa", "исп": "spa",
}

# Собственный крошечный URL-sniff — бот НЕ импортирует core. Классификацию
# youtube/direct/yandex делает resolve.py на стороне API; бот шлёт сырой url.
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Стадия одиночного элемента → текст статус-сообщения (эмодзи как в старом UX).
_STAGE_TEXT = {
    "download": "⬇️ Скачиваю…",
    "audio": "🎧 Извлекаю аудио…",
    "scribe": "📝 Транскрибирую через ElevenLabs Scribe…",
}


def _strip_media_ext(name: str) -> str:
    p = pathlib.PurePosixPath(name)
    while p.suffix.lower() in MEDIA_EXTS:
        p = p.with_suffix("")
    return p.name


def _parse_caption(text: str | None) -> tuple[int | None, dict[str, str], str | None]:
    """Parse caption like '2 Даниил Толя' → (2, {'speaker_0': 'Даниил', 'speaker_1': 'Толя'}, None).

    Strict: every token must be a digit, a known keyword, a language code, or a
    Capitalized name. If anything else appears, the whole caption is ignored.

    Supported:
        '2'              → 2 speakers, no names
        '1' or 'соло'    → 1 speaker (monologue)
        'Даниил Толя'    → 2 speakers, names auto-numbered
        '2 Даниил Толя'  → 2 speakers + names
        'en' / 'англ'    → force English (else Scribe auto-detects)
        empty / long / 'созвон с Толей' / '2 спикера' → no hints, model auto-detects
    """
    if not text:
        return None, {}, None
    text = text.strip()
    if not text or len(text) > 80:
        return None, {}, None

    tokens = [t for t in re.split(r"[,;/|\s]+", text) if t]
    if not tokens:
        return None, {}, None

    num: int | None = None
    names: list[str] = []
    language: str | None = None
    for t in tokens:
        low = t.lower()
        if t.isdigit():
            n = int(t)
            if not (1 <= n <= 32):
                return None, {}, None
            if num is None:
                num = n
            continue
        if low in SOLO_WORDS:
            if num is None:
                num = 1
            continue
        if low in DUO_WORDS:
            if num is None:
                num = 2
            continue
        if low in LANG_WORDS:
            if language is None:
                language = LANG_WORDS[low]
            continue
        # имя: ≥2 букв, начинается с заглавной, остальное — буквы/дефис/апостроф
        if len(t) >= 2 and t[0].isupper() and all(c.isalpha() or c in "-’'" for c in t):
            names.append(t)
            continue
        return None, {}, None  # неизвестный токен → бросаем всю команду

    if names and num is None:
        num = len(names)
    speaker_map = {f"speaker_{i}": n for i, n in enumerate(names)}
    return num, speaker_map, language


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("scribe-bot")


def _make_bot() -> Bot:
    props = DefaultBotProperties(parse_mode=ParseMode.HTML)
    # aiogram's default per-request HTTP timeout is 60s. A self-hosted Bot API
    # server downloads the whole file from Telegram on getFile (and large
    # uploads take time too), which blows 60s easily. Give it room.
    timeout = 30 * 60
    if LOCAL_API_URL:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(LOCAL_API_URL, is_local=True),
            timeout=timeout,
        )
        return Bot(TOKEN, session=session, default=props)
    return Bot(TOKEN, session=AiohttpSession(timeout=timeout), default=props)


bot = _make_bot()
dp = Dispatcher()

# aiohttp-сессия API-клиента. Создаётся в main() внутри running loop.
http: aiohttp.ClientSession | None = None

WELCOME = (
    "🎙 Видео и аудио становятся текстом — с абзацами и разделением по спикерам.\n\n"
    "<b>На входе:</b>\n"
    f"• видео / аудио / voice файлом — до {TG_FILE_LIMIT_LABEL}\n"
    "• ссылка на YouTube — любой длины\n"
    "• Яндекс.Диск, Google Drive или прямая http-ссылка\n\n"
    "<b>На выходе</b> — готовый <code>.txt</code>: чистый текст, разбитый на абзацы и по спикерам. "
    "Имя файла = название записи.\n\n"
    "<b>💬 Спикеры известны заранее?</b> Подпись к файлу или вторая строка после ссылки задаёт точнее:\n"
    "• <code>2</code> — диалог двоих\n"
    "• <code>соло</code> / <code>монолог</code> — один голос\n"
    "• имена через пробел — в порядке появления\n"
    "• <code>en</code> / <code>англ</code> — язык, если запись не на русском\n"
    "• пусто — определю сам\n\n"
    "Часовые записи — на YouTube unlisted или на Диск, и ссылкой. Скачаю сам."
)


def _allowed(user_id: int | None) -> bool:
    if not ALLOWED:
        return True
    return user_id in ALLOWED


def _user_tag(msg: Message) -> str:
    u = msg.from_user
    if not u:
        return "user=?"
    handle = f"@{u.username}" if u.username else (u.full_name or "—")
    return f"{handle}[{u.id}]"


def _media_kind(msg: Message) -> str:
    if msg.video: return "видео"
    if msg.audio: return "аудио"
    if msg.voice: return "голосовое"
    if msg.video_note: return "кружочек"
    if msg.document: return "документ"
    if msg.text: return "текст"
    return "сообщение"


async def _reject(msg: Message) -> None:
    await msg.answer("Извини, этот бот приватный.")
    log.info("rejected stranger: %s sent %s", _user_tag(msg), _media_kind(msg))
    await _notify_owner_of_stranger(msg)


async def _notify_owner_of_stranger(msg: Message) -> None:
    if not OWNER_ID or not msg.from_user:
        return
    u = msg.from_user
    name = html.escape(u.full_name or "—")
    who = f"<a href='tg://user?id={u.id}'>{name}</a>"
    if u.username:
        who += f" (@{html.escape(u.username)})"
    who += f" [<code>{u.id}</code>]"

    text = f"🚫 Чужой пишет боту\n\n{who}\nТип: {_media_kind(msg)}"
    if msg.text:
        preview = html.escape(msg.text[:300])
        text += f"\n<pre>{preview}</pre>"

    try:
        await bot.send_message(OWNER_ID, text, disable_web_page_preview=True)
    except Exception:
        log.exception("failed to notify owner")


@dp.message(Command("start", "help"))
async def on_start(msg: Message) -> None:
    if not _allowed(msg.from_user.id if msg.from_user else None):
        return await _reject(msg)
    await msg.answer(WELCOME)


@dp.message(F.video | F.audio | F.voice | F.video_note | F.document)
async def on_media(msg: Message) -> None:
    if not _allowed(msg.from_user.id if msg.from_user else None):
        return await _reject(msg)

    media = msg.video or msg.audio or msg.voice or msg.video_note or msg.document
    if not media:
        return

    size = getattr(media, "file_size", None) or 0
    name = getattr(media, "file_name", None) or ""
    tag = _user_tag(msg)
    log.info(
        "%s sent %s: name=%r size=%.2fMB",
        tag, _media_kind(msg), name, size / 1024 / 1024,
    )
    if size and size > TG_FILE_LIMIT:
        await msg.answer(
            f"Файл {size / 1024 / 1024:.1f} МБ — больше лимита ({TG_FILE_LIMIT_LABEL}).\n"
            "Залей на Яндекс.Диск или YouTube unlisted и пришли ссылку."
        )
        log.info("%s rejected: over TG limit (%.1fMB)", tag, size / 1024 / 1024)
        return

    num_speakers, speaker_names, language = _parse_caption(msg.caption)
    status = await msg.answer("⬇️ Принимаю файл…")
    try:
        # Стейджим входящий файл СРАЗУ под SCRIBE_STAGING_DIR/<uuid>/<name> — одним
        # shutil.move (та же ФС, что download-каталог Bot-API). Уникальный подкаталог
        # держит имя файла чистым → resolve.py вернёт человекочитаемый stem для .txt.
        stem = _stem_from(msg, media)
        ext = pathlib.Path(getattr(media, "file_name", "") or "").suffix
        dst_dir = STAGING_DIR / uuid.uuid4().hex
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"{stem}{ext or ''}"
        file = await bot.get_file(media.file_id)
        await _fetch_to(file.file_path, dst)
        # Удаление входа — на стороне API (после джобы). Бот НЕ чистит staging.
    except Exception as e:
        log.exception("%s file staging failed", tag)
        await status.edit_text(f"❌ Ошибка приёма файла: {e}")
        return

    await _run_job(
        msg, status, tag,
        local_path=str(dst.resolve()),
        language=language, num_speakers=num_speakers, speaker_names=speaker_names,
    )


@dp.message(F.text)
async def on_text(msg: Message) -> None:
    if not _allowed(msg.from_user.id if msg.from_user else None):
        return await _reject(msg)

    raw = (msg.text or "").strip()
    tag = _user_tag(msg)
    log.info("%s sent text: %r", tag, raw[:200])
    url, caption = _split_url_and_caption(raw)
    if not url or not URL_RE.match(url):
        await msg.answer(
            "Пришли видео/аудио файлом или ссылкой (YouTube / Яндекс.Диск / Google Drive)."
        )
        return

    num_speakers, speaker_names, language = _parse_caption(caption)
    status = await msg.answer("⬇️ Принимаю ссылку…")
    await _run_job(
        msg, status, tag,
        url=url,
        language=language, num_speakers=num_speakers, speaker_names=speaker_names,
    )


# --- API client -----------------------------------------------------------------

async def _create_job(
    *,
    local_path: str | None = None,
    url: str | None = None,
    language: str | None = None,
    num_speakers: int | None = None,
    speaker_names: dict[str, str] | None = None,
) -> str:
    """POST /jobs (Form): ровно один источник + подсказки (часть ключа кеша). → job_id."""
    assert http is not None
    data = aiohttp.FormData()
    if local_path:
        data.add_field("local_path", local_path)
    if url:
        data.add_field("url", url)
    if language:
        data.add_field("language", language)
    if num_speakers is not None:
        data.add_field("num_speakers", str(num_speakers))
    if speaker_names:
        data.add_field("speaker_names", json.dumps(speaker_names, ensure_ascii=False))
    async with http.post(f"{SCRIBE_API_URL}/jobs", data=data) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"POST /jobs → {resp.status}: {body[:300]}")
        payload = await resp.json()
    return payload["job_id"]


class _TransientAPIError(Exception):
    """GET /jobs упал транзиентно (connection refused / 5xx при рестарте API) → ретрай."""


async def _get_job(job_id: str) -> dict:
    """GET /jobs/{id}. 5xx / сетевой сбой → _TransientAPIError (ретрай); 404 → hard error."""
    assert http is not None
    try:
        async with http.get(f"{SCRIBE_API_URL}/jobs/{job_id}") as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 404:
                raise RuntimeError("джоба не найдена (404)")
            # 5xx (вкл. 503 «status temporarily unavailable») и всё прочее — транзиентно.
            raise _TransientAPIError(f"HTTP {resp.status}")
    except aiohttp.ClientError as e:  # connection refused / reset при рестарте API
        raise _TransientAPIError(str(e)) from e


def _render_progress(job: dict) -> str:
    """Текст статус-сообщения для НЕтерминальной джобы."""
    items = job.get("items") or []
    if len(items) <= 1:
        it = items[0] if items else None
        stage = (it or {}).get("stage") or "download"
        return _STAGE_TEXT.get(stage, "⬇️ Обрабатываю…")
    total = len(items)
    done = sum(1 for i in items if i.get("status") == "done")
    return f"Обрабатываю {total} элементов… готово {done}/{total}"


async def _run_job(
    msg: Message,
    status: Message,
    tag: str,
    *,
    local_path: str | None = None,
    url: str | None = None,
    language: str | None = None,
    num_speakers: int | None = None,
    speaker_names: dict[str, str] | None = None,
) -> None:
    """Создать джобу, поллить до терминала, отдать результат. Ретраит транзиентный GET."""
    try:
        job_id = await _create_job(
            local_path=local_path, url=url,
            language=language, num_speakers=num_speakers, speaker_names=speaker_names,
        )
    except Exception as e:
        log.exception("%s create job failed", tag)
        await status.edit_text(f"❌ Не удалось создать задачу: {e}")
        return
    log.info(
        "%s job %s created (local_path=%r url=%r num=%r lang=%r)",
        tag, job_id, local_path, url, num_speakers, language,
    )

    deadline = time.monotonic() + POLL_CEILING_SEC
    backoff = BACKOFF_START_SEC
    last_render: str | None = None
    while True:
        if time.monotonic() > deadline:
            log.warning("%s job %s exceeded polling ceiling", tag, job_id)
            await status.edit_text("❌ Задача не завершилась за отведённое время (6 часов).")
            return

        try:
            job = await _get_job(job_id)
            backoff = BACKOFF_START_SEC  # успех → сбрасываем backoff
        except _TransientAPIError as e:
            log.warning("%s job %s transient GET (%s) — retry in %ss", tag, job_id, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SEC)
            continue
        except Exception as e:
            log.exception("%s job %s GET failed", tag, job_id)
            await status.edit_text(f"❌ Ошибка получения статуса: {e}")
            return

        if job.get("status") in ("done", "error"):
            await _deliver(msg, status, tag, job)
            return

        render = _render_progress(job)
        if render != last_render:
            with contextlib.suppress(Exception):
                await status.edit_text(render)
            last_render = render
        await asyncio.sleep(POLL_INTERVAL_SEC)


async def _deliver(msg: Message, status: Message, tag: str, job: dict) -> None:
    """Терминальная джоба: один .txt на каждый done-элемент (in-memory), список ошибок."""
    items = job.get("items") or []
    done_items = [it for it in items if it.get("status") == "done"]
    error_items = [it for it in items if it.get("status") == "error"]

    if job.get("status") == "error":  # ни одного done (resolve пуст/исключение/все упали)
        err = job.get("error") or "не удалось обработать источник"
        await status.edit_text(f"❌ Ошибка: {err}")
        log.info("%s job failed: %s", tag, err)
        return

    await status.edit_text("✅ Готово, отправляю…")
    for it in done_items:
        text = it.get("text") or ""
        stem = it.get("stem") or "transcript"
        payload = text.encode("utf-8")
        await msg.answer_document(BufferedInputFile(payload, filename=f"{stem}.txt"))
        log.info("%s delivered %s.txt (%d bytes)", tag, stem, len(payload))

    if error_items:
        names = ", ".join(
            str(it.get("name") or it.get("stem") or f"#{it.get('index')}") for it in error_items
        )
        await msg.answer(f"⚠️ Не удалось обработать: {names}")
        log.info("%s %d item(s) failed: %s", tag, len(error_items), names)

    with contextlib.suppress(Exception):
        await status.delete()


# --- helpers --------------------------------------------------------------------

async def _fetch_to(file_path: str | None, dst: pathlib.Path) -> None:
    """In local-mode getFile returns absolute path on disk; move it into staging.
    Otherwise HTTP-download straight into the staging destination."""
    if LOCAL_API_URL and file_path:
        local_src = pathlib.Path(file_path)
        if local_src.is_file():
            shutil.move(str(local_src), str(dst))
            return
    await bot.download_file(file_path, destination=dst)


def _split_url_and_caption(text: str) -> tuple[str, str]:
    """Split 'URL\\ncaption' or 'URL caption' — first whitespace-separated token is URL."""
    text = text.strip()
    if not text:
        return "", ""
    parts = text.split(None, 1)
    url = parts[0]
    caption = parts[1].strip() if len(parts) == 2 else ""
    return url, caption


def _stem_from(msg: Message, media) -> str:
    name = getattr(media, "file_name", None)
    if name:
        return _strip_media_ext(name)[:80] or f"transcript_{msg.message_id}"
    return f"transcript_{msg.message_id}"


def _check_staging_fs() -> None:
    """WARNING на старте, если staging и download-каталог Bot-API на разных ФС —
    иначе shutil.move тихо деградирует в copy до 2 ГБ на каждую загрузку."""
    try:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("cannot create staging dir %s", STAGING_DIR)
        return
    if not BOTAPI_DOWNLOAD_DIR.exists():
        # Нет локального Bot-API download-каталога (облачный API / dev) — move не через
        # его границу, проверять нечего.
        log.info("BOTAPI_DOWNLOAD_DIR %s absent — skipping staging same-FS check", BOTAPI_DOWNLOAD_DIR)
        return
    try:
        if os.stat(STAGING_DIR).st_dev != os.stat(BOTAPI_DOWNLOAD_DIR).st_dev:
            log.warning(
                "SCRIBE_STAGING_DIR (%s) and BOTAPI_DOWNLOAD_DIR (%s) are on DIFFERENT "
                "filesystems — shutil.move will degrade to a full copy of every uploaded "
                "file (up to %s). Put staging on the same mount as the Bot-API download dir.",
                STAGING_DIR, BOTAPI_DOWNLOAD_DIR, TG_FILE_LIMIT_LABEL,
            )
    except OSError:
        log.exception("staging same-FS check failed")


async def main() -> None:
    global http
    _check_staging_fs()
    http = aiohttp.ClientSession()
    log.info(
        "starting polling (allowed users: %s, API=%s, staging=%s)",
        ALLOWED or "ALL", SCRIBE_API_URL, STAGING_DIR,
    )
    try:
        await dp.start_polling(bot)
    finally:
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
