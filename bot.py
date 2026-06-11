"""Telegram bot: receive video/audio/URL, transcribe via ElevenLabs Scribe, send back txt/srt/json."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import pathlib
import re
import shutil
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv

import downloader
import scribe
import storage

APP_ENV = os.environ.get("APP_ENV", "local")
load_dotenv(pathlib.Path(__file__).parent / f".env.{APP_ENV}")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ELEVEN_KEY = os.environ["ELEVENLABS_API_KEY"]
ALLOWED: set[int] = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
}
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or "0")
LOCAL_API_URL = os.environ.get("TG_LOCAL_API_URL", "").strip() or None
TG_FILE_LIMIT = 2 * 1024 * 1024 * 1024 if LOCAL_API_URL else 20 * 1024 * 1024
TG_FILE_LIMIT_LABEL = "2 ГБ" if LOCAL_API_URL else "20 МБ"
ARCHIVE_DIR = pathlib.Path(
    os.environ.get("SCRIBE_ARCHIVE_DIR") or (pathlib.Path(__file__).parent / "archive")
)

MEDIA_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v",
    ".3gp", ".ts", ".mts", ".mpeg", ".mpg", ".ogv",
    ".opus", ".ogg", ".oga", ".mp3", ".wav", ".flac", ".aac", ".m4a", ".wma",
}

SOLO_WORDS = {"соло", "solo", "монолог", "monolog", "monologue"}
DUO_WORDS = {"диалог", "dialog", "dialogue", "созвон", "call"}


def _strip_media_ext(name: str) -> str:
    p = pathlib.PurePosixPath(name)
    while p.suffix.lower() in MEDIA_EXTS:
        p = p.with_suffix("")
    return p.name


def _parse_caption(text: str | None) -> tuple[int | None, dict[str, str]]:
    """Parse caption like '2 Даниил Толя' → (2, {'speaker_0': 'Даниил', 'speaker_1': 'Толя'}).

    Strict: every token must be a digit, a known keyword, or a Capitalized name.
    If anything else appears, the whole caption is ignored (no hints).

    Supported:
        '2'              → 2 speakers, no names
        '1' or 'соло'    → 1 speaker (monologue)
        'Даниил Толя'    → 2 speakers, names auto-numbered
        '2 Даниил Толя'  → 2 speakers + names
        empty / long / 'созвон с Толей' / '2 спикера' → no hints, model auto-detects
    """
    if not text:
        return None, {}
    text = text.strip()
    if not text or len(text) > 80:
        return None, {}

    tokens = [t for t in re.split(r"[,;/|\s]+", text) if t]
    if not tokens:
        return None, {}

    num: int | None = None
    names: list[str] = []
    for t in tokens:
        low = t.lower()
        if t.isdigit():
            n = int(t)
            if not (1 <= n <= 32):
                return None, {}
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
        # имя: ≥2 букв, начинается с заглавной, остальное — буквы/дефис/апостроф
        if len(t) >= 2 and t[0].isupper() and all(c.isalpha() or c in "-’'" for c in t):
            names.append(t)
            continue
        return None, {}  # неизвестный токен → бросаем всю команду

    if names and num is None:
        num = len(names)
    speaker_map = {f"speaker_{i}": n for i, n in enumerate(names)}
    return num, speaker_map

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


def _archive(msg: Message, outputs: dict[str, pathlib.Path]) -> None:
    """Copy txt+json of a successful transcription into archive/<user_id>/<ts>_<stem>.{ext}."""
    user_id = msg.from_user.id if msg.from_user else 0
    try:
        ts = time.strftime("%Y-%m-%d_%H%M%S")
        dest = ARCHIVE_DIR / str(user_id)
        dest.mkdir(parents=True, exist_ok=True)
        for kind, p in outputs.items():
            if kind == "srt":
                continue
            if p.exists() and p.stat().st_size > 0:
                shutil.copy(p, dest / f"{ts}_{p.name}")
    except Exception:
        log.exception("archive failed for %s", _user_tag(msg))


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

    num_speakers, speaker_names = _parse_caption(msg.caption)
    workdir = storage.new_workdir()
    status = await msg.answer("⬇️ Скачиваю файл…")
    try:
        stem = _stem_from(msg, media)
        ext = pathlib.Path(getattr(media, "file_name", "") or "").suffix
        src = workdir / f"{stem}{ext or ''}"
        file = await bot.get_file(media.file_id)
        await _fetch_to(file.file_path, src)

        await status.edit_text("🎧 Извлекаю аудио…")
        audio = await downloader.extract_audio(src, workdir, stem=stem)

        await _transcribe_and_send(
            msg, status, audio, workdir, stem=stem,
            num_speakers=num_speakers, speaker_names=speaker_names,
        )
    except Exception as e:
        log.exception("%s media pipeline failed", tag)
        await status.edit_text(f"❌ Ошибка: {e}")
    finally:
        storage.cleanup(workdir)


@dp.message(F.text)
async def on_text(msg: Message) -> None:
    if not _allowed(msg.from_user.id if msg.from_user else None):
        return await _reject(msg)

    raw = (msg.text or "").strip()
    tag = _user_tag(msg)
    log.info("%s sent text: %r", tag, raw[:200])
    url, caption = _split_url_and_caption(raw)
    if not url or not downloader.is_url(url):
        await msg.answer(
            "Пришли видео/аудио файлом или ссылкой (YouTube / Яндекс.Диск / Google Drive)."
        )
        return

    num_speakers, speaker_names = _parse_caption(caption)
    workdir = storage.new_workdir()
    status = await msg.answer("⬇️ Скачиваю…")
    try:
        kind = "youtube" if downloader.is_youtube_url(url) else "direct-url"
        if downloader.is_youtube_url(url):
            src = await downloader.download_youtube(url, workdir)
        else:
            await status.edit_text("⬇️ Скачиваю файл…")
            src = await downloader.download_direct(url, workdir)
        log.info(
            "%s downloaded %s: name=%r size=%.2fMB",
            tag, kind, src.name, src.stat().st_size / 1024 / 1024,
        )

        raw_stem = _strip_media_ext(src.name)
        stem = raw_stem if raw_stem and raw_stem.lower() not in ("audio", "source") else _stem_from_url(url)

        await status.edit_text("🎧 Извлекаю аудио…")
        # Always downmix to mono opus — Scribe drops content on stereo sources.
        audio = await downloader.extract_audio(src, workdir, stem="audio")

        await _transcribe_and_send(
            msg, status, audio, workdir, stem=stem,
            num_speakers=num_speakers, speaker_names=speaker_names,
        )
    except Exception as e:
        log.exception("%s url pipeline failed", tag)
        await status.edit_text(f"❌ Ошибка: {e}")
    finally:
        storage.cleanup(workdir)


async def _transcribe_and_send(
    msg: Message,
    status: Message,
    audio: pathlib.Path,
    workdir: pathlib.Path,
    stem: str,
    *,
    num_speakers: int | None = None,
    speaker_names: dict[str, str] | None = None,
) -> None:
    tag = _user_tag(msg)
    duration = await downloader.probe_duration(audio)
    size_mb = audio.stat().st_size / 1024 / 1024
    info = f"{size_mb:.1f} МБ"
    if duration:
        h, rem = divmod(int(duration), 3600)
        m, s = divmod(rem, 60)
        info = f"{h:02d}:{m:02d}:{s:02d}, {size_mb:.1f} МБ"
    log.info(
        "%s audio ready: stem=%r %.2fMB %.0fs num_speakers=%r names=%r → scribe",
        tag, stem, size_mb, duration or 0.0, num_speakers, speaker_names,
    )
    hint = ""
    if num_speakers:
        hint = f", спикеров: {num_speakers}"
        if speaker_names:
            hint += f" ({', '.join(speaker_names.values())})"
    await status.edit_text(f"📝 Транскрибирую через ElevenLabs Scribe… ({info}{hint})")

    biased = list(speaker_names.values()) if speaker_names else None
    t0 = time.time()
    data = await asyncio.to_thread(
        scribe.transcribe, audio, ELEVEN_KEY,
        num_speakers=num_speakers, biased_keywords=biased,
    )
    scribe_sec = time.time() - t0
    outputs = scribe.write_outputs(data, workdir, stem, speaker_names=speaker_names)
    words = [w for w in data.get("words", []) if w.get("type") == "word"]
    txt = outputs["txt"]
    txt_bytes = txt.stat().st_size if txt.exists() else 0
    last_ts = words[-1].get("start", 0.0) if words else 0.0
    log.info(
        "%s scribe ok: %d words, last_ts=%.1fs, txt=%d bytes, took=%.1fs",
        tag, len(words), last_ts, txt_bytes, scribe_sec,
    )
    _archive(msg, outputs)

    await status.edit_text("✅ Готово, отправляю…")
    if txt.exists() and txt_bytes > 0:
        await msg.answer_document(FSInputFile(txt))
    await status.delete()


async def _fetch_to(file_path: str | None, dst: pathlib.Path) -> None:
    """In local-mode getFile returns absolute path on disk; move it. Otherwise HTTP-download."""
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


def _stem_from_url(url: str) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    last = _strip_media_ext(pathlib.PurePosixPath(parsed.path).name) or parsed.netloc.replace(".", "_")
    return last[:80] or "transcript"


async def _periodic_cleanup() -> None:
    while True:
        await asyncio.sleep(3600)
        removed = storage.cleanup_old(max_age_hours=6)
        if removed:
            log.info("cleanup: removed %d stale workdirs", removed)


async def main() -> None:
    asyncio.create_task(_periodic_cleanup())
    log.info("starting polling (allowed users: %s)", ALLOWED or "ALL")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
