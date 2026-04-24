"""Telegram bot: receive video/audio/URL, transcribe via ElevenLabs Scribe, send back txt/srt/json."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import pathlib

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv

import downloader
import scribe
import storage

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ELEVEN_KEY = os.environ["ELEVENLABS_API_KEY"]
ALLOWED: set[int] = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
}
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or "0")
TG_FILE_LIMIT = 20 * 1024 * 1024  # stock Bot API limit

MEDIA_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v",
    ".3gp", ".ts", ".mts", ".mpeg", ".mpg", ".ogv",
    ".opus", ".ogg", ".oga", ".mp3", ".wav", ".flac", ".aac", ".m4a", ".wma",
}


def _strip_media_ext(name: str) -> str:
    p = pathlib.PurePosixPath(name)
    while p.suffix.lower() in MEDIA_EXTS:
        p = p.with_suffix("")
    return p.name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("scribe-bot")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

WELCOME = (
    "Привет! Я расшифровываю видео и аудио в текст через ElevenLabs Scribe.\n\n"
    "<b>Что можно прислать:</b>\n"
    "• Видео/аудио/voice файлом (до 20 МБ — это ограничение Telegram)\n"
    "• Ссылку на YouTube — любой длины\n"
    "• Прямую ссылку на файл (Яндекс.Диск, Google Drive, прямой http)\n\n"
    "В ответ пришлю <code>.txt</code> с текстом, разбитым на абзацы и по спикерам. "
    "Имя файла = название видео.\n\n"
    "Длинные видео (несколько часов) лучше заливать на YouTube unlisted или на Диск, и присылать ссылку."
)


def _allowed(user_id: int | None) -> bool:
    if not ALLOWED:
        return True
    return user_id in ALLOWED


async def _reject(msg: Message) -> None:
    await msg.answer("Извини, этот бот приватный.")
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

    if msg.video:
        kind = "видео"
    elif msg.audio:
        kind = "аудио"
    elif msg.voice:
        kind = "голосовое"
    elif msg.video_note:
        kind = "кружочек"
    elif msg.document:
        kind = "документ"
    elif msg.text:
        kind = "текст"
    else:
        kind = "сообщение"

    text = f"🚫 Чужой пишет боту\n\n{who}\nТип: {kind}"
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
    if size and size > TG_FILE_LIMIT:
        await msg.answer(
            f"Файл {size / 1024 / 1024:.1f} МБ — больше лимита Telegram Bot API (20 МБ).\n"
            "Залей на Яндекс.Диск или YouTube unlisted и пришли ссылку."
        )
        return

    workdir = storage.new_workdir()
    status = await msg.answer("⬇️ Скачиваю файл…")
    try:
        stem = _stem_from(msg, media)
        ext = pathlib.Path(getattr(media, "file_name", "") or "").suffix
        src = workdir / f"{stem}{ext or ''}"
        file = await bot.get_file(media.file_id)
        await bot.download_file(file.file_path, destination=src)

        await status.edit_text("🎧 Извлекаю аудио…")
        audio = await downloader.extract_audio(src, workdir, stem=stem)

        await _transcribe_and_send(msg, status, audio, workdir, stem=stem)
    except Exception as e:
        log.exception("media pipeline failed")
        await status.edit_text(f"❌ Ошибка: {e}")
    finally:
        storage.cleanup(workdir)


@dp.message(F.text)
async def on_text(msg: Message) -> None:
    if not _allowed(msg.from_user.id if msg.from_user else None):
        return await _reject(msg)

    text = (msg.text or "").strip()
    if not downloader.is_url(text):
        await msg.answer(
            "Пришли видео/аудио файлом или ссылкой (YouTube / Яндекс.Диск / Google Drive)."
        )
        return

    workdir = storage.new_workdir()
    status = await msg.answer("⬇️ Скачиваю…")
    try:
        if downloader.is_youtube_url(text):
            audio = await downloader.download_youtube(text, workdir)
        else:
            await status.edit_text("⬇️ Скачиваю файл…")
            src = await downloader.download_direct(text, workdir)
            if src.suffix.lower() == ".opus":
                audio = src
            else:
                await status.edit_text("🎧 Извлекаю аудио…")
                audio = await downloader.extract_audio(src, workdir, stem=src.stem)

        raw_stem = _strip_media_ext(audio.name)
        stem = raw_stem if raw_stem and raw_stem.lower() not in ("audio", "source") else _stem_from_url(text)
        await _transcribe_and_send(msg, status, audio, workdir, stem=stem)
    except Exception as e:
        log.exception("url pipeline failed")
        await status.edit_text(f"❌ Ошибка: {e}")
    finally:
        storage.cleanup(workdir)


async def _transcribe_and_send(
    msg: Message,
    status: Message,
    audio: pathlib.Path,
    workdir: pathlib.Path,
    stem: str,
) -> None:
    duration = await downloader.probe_duration(audio)
    size_mb = audio.stat().st_size / 1024 / 1024
    info = f"{size_mb:.1f} МБ"
    if duration:
        h, rem = divmod(int(duration), 3600)
        m, s = divmod(rem, 60)
        info = f"{h:02d}:{m:02d}:{s:02d}, {size_mb:.1f} МБ"
    await status.edit_text(f"📝 Транскрибирую через ElevenLabs Scribe… ({info})")

    data = await asyncio.to_thread(scribe.transcribe, audio, ELEVEN_KEY)
    outputs = scribe.write_outputs(data, workdir, stem)

    await status.edit_text("✅ Готово, отправляю…")
    txt = outputs["txt"]
    if txt.exists() and txt.stat().st_size > 0:
        await msg.answer_document(FSInputFile(txt))
    await status.delete()


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
