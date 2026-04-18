# scribe-it

Telegram bot that transcribes video/audio via ElevenLabs Scribe.

## Input
- Telegram audio/video/voice file (≤ 20 MB, stock Bot API limit)
- YouTube URL
- Direct URL to audio/video (Yandex.Disk public link, Google Drive, etc.)

## Output
- `.txt` — plain text with speaker diarization and paragraph breaks
- `.srt` — subtitles with word-level timestamps
- `.json` — raw Scribe response

## Env vars
```
TELEGRAM_BOT_TOKEN=...
ELEVENLABS_API_KEY=...
ALLOWED_USER_IDS=123456,789012   # comma-separated Telegram user IDs
```

## Run locally
```
uv sync
uv run python bot.py
```

## Deploy
See `deploy/deploy.sh` and `deploy/scribe-bot.service`.
