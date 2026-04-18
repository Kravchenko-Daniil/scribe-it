# scribe-it

Telegram-бот, который расшифровывает видео и аудио в текст через ElevenLabs Scribe.

Telegram: [@daniil_scribe_bot](https://t.me/daniil_scribe_bot)

## Что принимает

| Формат | Лимит | Как работает |
|---|---|---|
| Файл (видео/аудио/voice/document) | до 20 МБ | Стандартное Telegram Bot API |
| Ссылка на YouTube | любая длина | `yt-dlp` скачивает аудио → Opus |
| Прямая ссылка (Яндекс.Диск, Google Drive, http) | любой размер | `yt-dlp` в универсальном режиме |

Большие видео (более 20 МБ) лучше заливать на YouTube (unlisted) или Яндекс.Диск и присылать боту ссылку.

## Что отдаёт

Для каждого запроса бот возвращает три файла:

- `*.txt` — чистый текст с разделением на абзацы и спикеров
- `*.srt` — субтитры с таймкодами на уровне слов
- `*.json` — сырой ответ от ElevenLabs Scribe (полная информация о словах, спикерах, времени)

## Как всё устроено

```
Telegram → bot.py (aiogram) → downloader.py → scribe.py → отправка файлов обратно
                                    │            │
                                    │            └── ElevenLabs Scribe API
                                    │
                                    ├── yt-dlp (YouTube / прямые ссылки)
                                    ├── ffmpeg (извлечение аудио → Opus 32 kbps)
                                    ├── bgutil-provider (Docker, PO-токены для YouTube)
                                    └── cookies.txt (YouTube сессия, см. ниже)
```

### Компоненты на VPS (Hetzner, `vps-master`)

- `scribe-bot.service` — systemd unit, запускает `uv run python bot.py`
- `bgutil-provider` — Docker-контейнер на `127.0.0.1:4416`, генерит YouTube PO-токены
- `deno` — runtime для JavaScript-challenges YouTube
- `ffmpeg` — извлечение аудио
- `/opt/scribe-bot/` — код бота и venv

### Почему не Aeza

ElevenLabs блокирует IP российских хостеров (включая Aeza) на уровне AS-номера, даже если сервер физически в Европе. Перенесли на Hetzner.

## Переменные окружения (`.env`)

```env
TELEGRAM_BOT_TOKEN=xxx:yyy
ELEVENLABS_API_KEY=sk_xxx
ALLOWED_USER_IDS=123456789,987654321    # опционально — whitelist, через запятую
```

Если `ALLOWED_USER_IDS` пустой — бот отвечает всем, кто найдёт (в credits будет дыра).

## Локальный запуск (для разработки)

```bash
cd D:\proj\scribe-bot
uv sync
uv run python bot.py
```

Перед запуском локально: установить `ffmpeg` (через apt/choco/brew) и убедиться что в `.env` лежат валидные токены.

## Деплой на VPS

```bash
./deploy/deploy.sh
```

Скрипт делает:
1. `git push origin main`
2. `rsync` файлов на VPS (`vps-master:/opt/scribe-bot/`)
3. `uv sync` на VPS
4. `systemctl restart scribe-bot`

## Cookies для YouTube

YouTube блокирует анонимные запросы с VPS. Для обхода — **cookies.txt** (залогиненная сессия YouTube):

1. В Chrome залогинься в YouTube (**отдельным аккаунтом**, не основным)
2. Поставь расширение [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
3. На странице youtube.com → кликни расширение → Export → скачается `cookies.txt`
4. Залей на VPS:
   ```bash
   scp cookies.txt vps-master:/opt/scribe-bot/cookies.txt
   ssh vps-master "chmod 600 /opt/scribe-bot/cookies.txt && systemctl restart scribe-bot"
   ```

Бот автоматически подхватит `cookies.txt` если он есть. `yt-dlp` сам продлевает сессию при каждом запросе, поэтому cookies живут долго (недели-месяцы).

Если в логах появится "Sign in to confirm you're not a bot" — cookies протухли, повторить шаги выше.

## Логи и отладка

```bash
# статус
ssh vps-master "systemctl status scribe-bot"

# живой поток логов
ssh vps-master "journalctl -u scribe-bot -f"

# последние 100 строк
ssh vps-master "journalctl -u scribe-bot -n 100 --no-pager"

# bgutil-контейнер
ssh vps-master "docker logs bgutil-provider --tail 20"
```

## Структура проекта

```
scribe-bot/
├── bot.py              # aiogram handlers, диспетчеризация сообщений
├── scribe.py           # отправка аудио в ElevenLabs Scribe → txt/srt/json
├── downloader.py       # yt-dlp + ffmpeg, извлечение аудио в Opus
├── storage.py          # управление tmp/-папками, автоочистка
├── pyproject.toml      # зависимости (uv)
├── .env                # секреты (в .gitignore)
├── cookies.txt         # YouTube-сессия (только на VPS, в .gitignore)
├── tmp/                # рабочие файлы во время обработки
└── deploy/
    ├── scribe-bot.service
    └── deploy.sh
```

## Стек

- Python 3.12, aiogram 3, uv
- yt-dlp + bgutil-ytdlp-pot-provider + deno
- ffmpeg
- ElevenLabs Scribe v1 API
- Ubuntu 24.04 LTS на Hetzner, systemd
