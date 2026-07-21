# scribe-it — план миграции

Как превратить текущий репозиторий в продукт из `docs/SPEC.md`. Этот файл — точка входа для продолжения работы в новой сессии: открыть SPEC.md (что строим) + этот файл (как и в каком порядке).

Статус: Фазы 1-4 реализованы (core/ + api.py + тонкий bot.py + конфиг/деплой/доки); проверки `tests/check_phase1..3.py` зелёные. Фаза 5 (сайт) — позже. Прод-катовер выполняет оркестратор отдельно.

## Принятые решения (развилки закрыты)
- **Job store** — каталог на диске (`job-id → status.json + items`). Переживает рестарт API, бот не теряет поллируемую джобу.
- **archive/** — удаляется как концепт. Кеш (TTL 30д, дедуп по хешу) покрывает «вернуться к материалу». Browsable пер-юзер историю не сохраняем. Это значит: удалить и `_archive`-функцию, и константу `ARCHIVE_DIR`/чтение `SCRIBE_ARCHIVE_DIR` (bot.py:38-39), и строку `archive/` из `.gitignore`.
- **Кеш** — только по хешу opus (ключ Scribe-шага). URL pre-key не вводим: повтор ссылки на длинное видео всё равно качается+ffmpeg заново, экономим только оплату Scribe.
- **arsen_batch.py** — выносится как есть и замораживается как мёртвый (разовая личная задача уже выполнена). Импорты `downloader`/`scribe` после Фазы 1 не чиним.

## Фиксированные константы (источник правды для кода)
Чтобы api.py / bot.py / deploy.sh не разошлись, всё ниже задаётся **через env с дефолтом**, путь — абсолютный, НЕ `__file__`-relative (иначе мигрирует с пакетом при переносе в `core/`).
- `SCRIBE_TMP_DIR`   — рабочие workdir'ы. Дефолт `/opt/scribe-bot/tmp` (prod) / `<repo>/tmp` (local). Владелец очистки — API (`cleanup_old`).
- `SCRIBE_CACHE_DIR` — кеш Scribe JSON. Дефолт `/opt/scribe-bot/cache`.
- `SCRIBE_STAGING_DIR` — переданные ботом файлы Telegram. Дефолт `/opt/scribe-bot/staging`. **Должен быть на той же ФС, что и download-каталог локального Bot-API** (см. Фаза 3 и «Якорь staging-ФС» ниже) — иначе move через границу ФС превращается в copy 2 ГБ и теряется инвариант «без перезаливки» (SPEC.md:54).
- `SCRIBE_JOBS_DIR`  — job store. Дефолт `/opt/scribe-bot/jobs`.
- `COOKIES_PATH`     — cookies.txt для yt-dlp. Дефолт `<repo-root>/cookies.txt` (= `pathlib.Path(__file__).resolve().parent.parent / "cookies.txt"` из `core/download.py`).
- `SCRIBE_API_URL`   — базовый URL API для бота. Дефолт `http://127.0.0.1:8080`.
- `SCRIBE_API_HOST` / `SCRIBE_API_PORT` — bind uvicorn. Дефолт `127.0.0.1` / `8080`. **Должны совпадать с хвостом `SCRIBE_API_URL` и с curl health-loop в deploy.sh** (uvicorn по умолчанию слушает :8000 — без явного `--port 8080` health-loop и бот стучатся в пустой порт и деплой падает всегда).
- `SCRIBE_CONCURRENCY` — значение глобального семафора (см. Фаза 2). Дефолт `2`.
- `HEALTH_RETRIES` / `HEALTH_INTERVAL_SEC` — бюджет health-поллинга деплоя. Дефолт `30` × `2` c (= ~60 c на подъём uvicorn). См. Фаза 4.

Все четыре runtime-каталога (`tmp`, `cache`, `staging`, `jobs`) лежат под `REMOTE_DIR` → **все четыре** в `--exclude` rsync и в `.gitignore` (см. Фаза 4). Если вынести их за `REMOTE_DIR` — exclude не нужен, но тогда зафиксировать абсолютный путь вне `/opt/scribe-bot`. Решение: оставляем под `REMOTE_DIR`, исключаем все четыре.

**Якорь staging-ФС (источник правды для same-FS инварианта):** локальный telegram-bot-api пишет скачанные файлы в свой download-каталог, который задаётся при запуске bot-api-сервера ВНЕ этого репо. На проде он зафиксирован как `/opt/scribe-bot/botapi` (документировать рядом с unit-файлом bot-api). `SCRIBE_STAGING_DIR` по дефолту = подкаталог под той же точкой монтирования (`/opt/scribe-bot/staging`, та же ФС, что `/opt/scribe-bot/botapi`). На старте **бот логирует WARNING и не делает move-as-copy втихую**, если `os.stat(SCRIBE_STAGING_DIR).st_dev != os.stat(<botapi_download_dir>).st_dev` — рассинхрон ФС всплывает громко, а не 2-гигабайтным copy на каждую загрузку. `TELEGRAM_LOCAL` из .env.prod удаляется только после подтверждения, что он не конфигурит этот download-каталог (в коде он не читается; бот читает `TG_LOCAL_API_URL`).

## Целевая структура
```
api.py            HTTP-сервис: POST /jobs, GET /jobs/{id}, GET /health, job-модель, владеет кешем/staging/tmp/jobs и очисткой
core/             единственное место логики
  resolve.py      источник → список MediaItem (без скачивания): файл / YouTube видео+плейлист / Яндекс файл+папка / Drive+http
  download.py     бывший downloader.py (yt-dlp, ffmpeg→opus, probe, yandex download_to) — целиком; cookies-путь чинится (см. Фаза 1)
  scribe.py       бывший scribe.py (Scribe-вызов + рендер txt/srt/json) — целиком
  storage.py      бывший storage.py (workdir под SCRIBE_TMP_DIR: new_workdir/cleanup/cleanup_old)
  cache.py        НОВОЕ: ключ = sha256(opus) + hash(scribe-params); хранит сырой JSON; TTL 30д; фоновая эвикция
bot.py            тонкий HTTP-клиент API (Telegram I/O + allow-list + локальный URL-sniff + отправка .txt по элементам)
```
Сайт — отдельный клиент, делается позже (Фаза 5).

## Контракты (зафиксированы, чтобы api.py/bot.py/resolve.py совпали)

**MediaItem** (что возвращает `resolve.py`, БЕЗ скачивания — скачивание владеет job-loop):
```
MediaItem(
  index: int,           # 0-based позиция в списке, замораживается при создании джобы; ключ файла items/<index>.txt
  name: str,            # человекочитаемое отображаемое имя (может нести исходное расширение, напр. "1.mp4")
  kind: Literal["youtube","yandex","direct","file"],
  ref: str,             # url ролика / yandex (public_key,path) сериализованный / абсолютный local_path
  stem: str,            # уникальный per-item stem (slugify(name) с обрезанным медиа-расширением; при коллизии + индекс). Имя выдаваемого .txt = "<stem>.txt"
)
```
Скачивание по kind (см. «Пайплайн на элемент» ниже): `youtube`→`download_youtube(ref, item_workdir)` (`--no-playlist`); `yandex`→`get_download_url`+`download_to`; `direct`→`download_direct`; `file`→локальный путь (staging). **Каждый элемент — свой workdir** (иначе `download_youtube`/`download_direct` сканируют каталог и берут первый файл — ломается на N).

**Пайплайн на элемент (фиксирован, источник правды для resolve→download→hash):**
Каждый kind проходит ДВЕ стадии и завершается одной и той же нормализацией:
1. `download_*` → `src` (контейнер/опус как его отдал источник — для youtube это `download_youtube` с `--audio-format opus --audio-quality 0`, НЕ моно 32k; это промежуточный `src`, не хешируемый opus).
2. `extract_audio(src, item_workdir)` → **opus 32k mono** (`-vn -c:a libopus -b:a 32k -ac 1`, downloader.py:104-111). Это и есть единственный хешируемый выход; моно-даунмикс обязателен (Scribe теряет текст на стерео, bot.py:327-328).
3. `opus_hash = sha256` именно этого файла после `extract_audio`.
Без шага 2 для youtube хешировался бы quality-0-stereo opus → и хуже качество Scribe, и нестабильный hash. Все kind терминируются в `extract_audio`.

**Классификатор источника в resolve.py (фиксирован, паритет с сегодня):**
- **Извлечение public_key из Яндекс-URL:** весь URL вида `https://disk.yandex.ru/d/<id>` или `https://disk.yandex.ru/i/<id>` целиком передаётся как `public_key` в cloud-api `public/resources` (так же, как `list_folder` сегодня принимает публичный URL целиком, yadisk_batch.py:44). Отдельного парсинга id не делаем.
- **Дискриминатор папка↔файл — наличие `_embedded` в ответе `public/resources`** (не «несколько файлов»): `type=="dir"` (есть `_embedded.items`) → `kind="yandex"` для **всех** его файлов, включая папку из одного файла (cloud-api `list_folder`, **НЕ yt-dlp**); `ref` = сериализованный `(public_key, path)` каждого файла. `type=="file"` (нет `_embedded`) → одиночный файл → `kind="direct"` → `download_direct` (yt-dlp), как сегодня (bot.py:312-317, downloader.py:66-81). Папка из одного файла остаётся на cloud-api осознанно — это держит opus-hash стабильным (тот же файл всегда одним downloader'ом, см. ниже).
- YouTube-ролик/плейлист → `kind="youtube"`; Drive/прямой http → `kind="direct"`.
- **Плейлист YouTube:** развернуть через `yt-dlp --flat-playlist --print id` (или `--dump-single-json`) в N URL роликов → N MediaItem kind=`youtube`.
- **Одна download-функция на kind** (не смешивать yt-dlp и cloud-api для одного источника): это и делает opus-hash стабильным (см. ниже).

**status.json** (`<SCRIBE_JOBS_DIR>/<job_id>/status.json`), `job_id = uuid4().hex`:
```json
{
  "status": "queued|running|done|error",
  "error": null,
  "items": [
    { "index": 0, "name": "1.mp4", "status": "queued|running|done|error", "stage": null, "error": null, "opus_hash": null }
  ]
}
```
- `stage` ∈ `null|"download"|"audio"|"scribe"` — core пишет его перед каждой стадией элемента; нужен боту для прогресса (см. Фаза 3). Терминальные `done`/`error` его обнуляют/игнорируют.
- Текст элемента НЕ хранится в status.json. По завершении элемента рендерится в `<job_dir>/items/<index>.txt` (по полю `index`, не по позиции в массиве) из кешированного Scribe JSON. `GET /jobs/{id}` читает status.json и инлайнит текст каждого `done`-элемента из `items/<index>.txt` в поле `text` (для `error`-элементов `text=""`). Так poll-путь бота и GET-хендлер берут текст из одного места.
- **Запись `items/<index>.txt` атомарна** (`.txt.tmp` + `os.replace`) и происходит **до** флипа `items[i].status` в `done` — GET никогда не читает отсутствующий/полузаписанный txt для `done`-элемента.

**Запись status.json (единственный писатель + атомарность):** пишет только task самой джобы (элементы внутри джобы идут последовательно — конкурентных писателей нет). Каждая запись — `status.json.tmp` + `os.replace` (атомарная подмена). `GET /jobs/{id}` на чтении при битом JSON делает один ретрай чтения (на случай гонки с подменой), а не падает.

**Ключ кеша** (разрешение конфликта speaker_names ↔ biased): в коде `biased = list(speaker_names.values())` уходит в Scribe (`scribe.py:169`) и **меняет ответ**. Значит:
- В ключ входит ровно то, что меняет HTTP-запрос к Scribe: `language`, `num_speakers`, `biased_keywords = sorted(speaker_names.values())`.
- `speaker_id → display-name` карта (`_label`, `scribe.py:26`) — **только рендер**, в ключ НЕ входит: смена отображаемого имени не должна инвалидировать кеш.
- Сериализация ключа: `param_hash = sha256(json.dumps({"language":..,"num_speakers":..,"biased":sorted(...)}, sort_keys=True, ensure_ascii=False).encode())`. Файл кеша: `<SCRIBE_CACHE_DIR>/<opus_hash>_<param_hash>.json`.
- `opus_hash = sha256` полных байт opus, потоково по чанкам, hex digest, считается в core сразу после `extract_audio`. **Хеширование гонится через `asyncio.to_thread`** (sha256 2-гигабайтного opus — CPU-bound; на event-loop он застопорит все джобы и 5-секундные поллы бота). Хешируем **выход ffmpeg** (opus), не источник. Принято осознанно: разные версии ffmpeg/libopus на разных хостах → разный байт → промахи кеша между хостами допустимы (один прод-хост — несущественно). **Стабильность opus-hash на одном хосте гарантируется тем, что каждый source-kind всегда идёт одним и тем же downloader'ом + одинаковыми ffmpeg-флагами** (см. классификатор выше): иначе один и тот же файл, пришедший раз через cloud-api, раз через yt-dlp, дал бы разный pre-ffmpeg контейнер → разный байт → ложный промах.
- **Оговорка по `direct`/yt-dlp:** `download_direct` не пинит `-f` (downloader.py:70-75), yt-dlp может выбрать разный формат-контейнер run-to-run → разный pre-ffmpeg байт → промах кеша. Гарантия «повтор того же аудио из кеша» строго держится для `file`/`youtube`/`yandex`-папки; для `direct` хит — best-effort (промах = повторная оплата, не ошибка). Если позже захотим жёсткий хит на direct — пинить `-f` в `download_direct`.

## Фазы

### Фаза 0 — безопасная чистка (логику не трогаем)
- Вынести в `D:\lab\content`: `arsen_batch.py`, `arsen_state/`, `arsen_user.session` (личный скрап TG-канала; сессия = креды, унести целиком, не оставлять копию).
  - **arsen замораживается как мёртвый.** arsen_batch.py:34-36 делает `sys.path.insert(0, ROOT)` + `from downloader import ...` / `from scribe import ...`; после выноса + Фазы 1 (модули → `core.*`) эти импорты резолвятся в ничто. Чинить не будем — задача разовая и выполнена. Записать это рядом с вынесенным скриптом, чтобы не выглядело «рабочим».
- Удалить: `__pycache__/`.
- **НЕ удалять `yadisk_batch.py` здесь** — в нём единственная логика листинга папки Яндекса (см. Фаза 1). Его удаление перенесено в конец Фазы 1.

### Фаза 1 — `core/`
- Создать пакет `core/`, перенести `downloader.py`/`scribe.py`/`storage.py` целиком (аудит: ни одной «батч-приватной» функции, режем 0 — проверено).
- **Чинить cookies-путь при переносе `downloader.py`→`core/download.py`:** сегодня `cookies = pathlib.Path(__file__).parent / "cookies.txt"` (downloader.py:41). После переноса `__file__.parent` = `core/` → cookies.txt не найдётся, `download_youtube` молча уйдёт на PO-token путь и начнёт падать на cookie-gated видео (прод-регресс YouTube). Заменить на `COOKIES_PATH` (env, дефолт repo-root: `pathlib.Path(__file__).resolve().parent.parent / "cookies.txt"`). cookies.txt — gitignored в корне.
- **Чинить workdir-root при переносе `storage.py`→`core/storage.py`:** сегодня `ROOT = pathlib.Path(__file__).parent / "tmp"` (storage.py:9). После переноса станет `core/tmp/` — другой каталог, мимо deploy-exclude. Заменить на `SCRIBE_TMP_DIR` (env, дефолт `/opt/scribe-bot/tmp`).
- **Сохранить URL-сниффы для бота:** `is_url`/`is_youtube_url`/`URL_RE`/`YOUTUBE_RE` (downloader.py:9-21) переносятся в `core/download.py`, НО бот их больше не импортирует (Фаза 3 режет импорт core у бота). Бот получает собственную крошечную копию `URL_RE` (см. Фаза 3) — это единственная логика, дублируемая осознанно, чтобы тонкий клиент не зависел от core.
- **Добавить `core/resolve.py`** — источник → `list[MediaItem]`, БЕЗ скачивания, по классификатору из блока «Контракты»:
  - **Папка Яндекс.Диска:** портировать `list_folder`/`get_download_url`/`download_to` + `slugify`/`INVALID_FS` из `yadisk_batch.py:33-64` в `resolve.py` (листинг → MediaItem'ы kind=`yandex`) и `download.py` (`download_to`). Использует `cloud-api.yandex.net/v1/disk/public/resources`. Дискриминатор папка↔файл — наличие `_embedded` (см. классификатор). Имена → `slugify` перед stem.
  - Одиночные (`youtube`/`direct`/`file`) → список из одного MediaItem.
  - Каждому MediaItem проставляется `index` = его 0-based позиция в итоговом списке.
- **Добавить `core/cache.py`** (Scribe-step кеш, см. блок «Ключ кеша» выше):
  - Layout: один JSON на ключ, имя `<opus_hash>_<param_hash>.json`, тело = сырой ответ Scribe. Каталог `SCRIBE_CACHE_DIR`.
  - API модуля: `key(opus_path, language, num_speakers, biased) -> str`, `get(key) -> dict|None`, `put(key, data)`, `evict()`.
  - **TTL 30 дней по `mtime`, проверяется на чтении.** `get(key)` сам сверяет возраст файла: если `now - mtime > 30д` → возвращает `None` (промах). При попадании `get` **обновляет mtime** (`os.utime`) → скользящий TTL. Фоновая `evict()` (раз в час в api.py) физически удаляет файлы старше TTL.
  - txt/srt пересобираются на чтении через `to_paragraphs`/`to_srt`; `speaker_names` применяются на рендере из JSON.
  - **Порядок:** download → ffmpeg→opus → `sha256(opus)` (в to_thread) → lookup → при попадании пропустить Scribe. «Мгновенно/без оплаты» (SPEC.md:48) верно только для шага Scribe; download+ffmpeg повторяются (URL pre-key сознательно не вводим).
- ПЕРЕНЕСТИ ЗНАНИЕ из бота, не выкинуть: ветку youtube↔direct и моно-даунмикс (bot.py:327-328).
- **ПОСЛЕ того как resolve.py демонстрирует листинг папки Яндекса — удалить `yadisk_batch.py`** (знание перенесено).
- **Гейт верификации (конец Фазы 1, чистое ядро, БЕЗ api.py):**
  - golden-file — взять известный Scribe `.json`, ассертить, что `to_paragraphs`/`to_srt` дают байт-в-байт идентичный вывод до и после переноса (рендер тонкий: `PARAGRAPH_PAUSE_SEC=20`, `_clean`, `_split_long`).
  - cookies-чек: после переноса `download_youtube` находит `COOKIES_PATH` (ассерт, что путь резолвится в существующий cookies.txt при наличии).
  - Ручной end-to-end (файл + YouTube видео + Яндекс-папка) — **НЕ здесь**: требует запущенного API, переносится в конец Фазы 2 (см. ниже), т.к. `api.py` ещё не существует на этом рубеже.

### Фаза 2 — `api.py`
- `POST /jobs` вход (precedence, при >1 → 400, при 0 → 400):
  1. `local_path` (быстрый путь одного хоста, основной для бота) → kind=`file`.
  2. multipart-файл (фолбэк/сайт).
  3. `url`.
  Плюс опц. подсказки спикеров **и язык** (`language`). → `{job_id}`. Возвращает `queued` мгновенно.
  - **Валидация `local_path` (безопасность):** API на приёме делает `Path(local_path).resolve()` и требует `is_relative_to(SCRIBE_STAGING_DIR)` → иначе **400** (клиент не должен мочь подсунуть `/opt/scribe-bot/cookies.txt` и т.п.). Удаление в `finally` (см. ниже) тоже только для путей под `SCRIBE_STAGING_DIR`.
  - **Где живёт resolve и 400 «не распознанный источник»:** resolve запускается **внутри async job-task** (после возврата `queued`), не на приёме POST — длинный листинг плейлиста/папки не должен держать POST открытым. Поэтому «нераспознанный источник» не 400, а терминальный `status="error"` джобы (бот его ретранслирует). На приёме POST 400 даётся только за явные структурные ошибки входа (0 или >1 источника, local_path вне staging).
- `GET /jobs/{id}` → `{status: queued|running|done|error, items:[{index, name, text, status, stage, error}], error}`. `text` инлайнится из `items/<index>.txt`.
- `GET /health` — для health-check деплоя; должен 200 только если API прочитал `ELEVENLABS_API_KEY` (иначе катовер не должен пройти, см. Фаза 4).
- **Job-модель:**
  - **Хранилище:** `SCRIBE_JOBS_DIR/<job_id>/` (`status.json` + `items/`), `job_id=uuid4().hex`. Рестарт API не сиротит поллируемую джобу.
  - **Исполнение:** фоновая `asyncio`-задача на джобу; блокирующее ядро (`scribe.transcribe` — синхронный `requests.post`, scribe.py:173, таймаут 6 ч) и `sha256(opus)` гнать через `asyncio.to_thread`. Перед каждой стадией элемента писать `items[i].stage` (`download`/`audio`/`scribe`) в status.json (атомарно, см. контракт).
  - **Конкурентность:** глобальный `asyncio.Semaphore(SCRIBE_CONCURRENCY)` (дефолт 2) гейтит тяжёлую per-item стадию (download+ffmpeg+Scribe). Элементы внутри одной папки/плейлиста идут **последовательно**; семафор ограничивает кросс-джобную параллельность → суммарно in-flight Scribe ≤ N.
- **Терминальный статус джобы (полное определение, дыр нет):**
  - `resolve` вернул **пустой** список (напр. Яндекс-папка отфильтровалась в 0 файлов) → `status="error"`, `error="источник пуст / не распознан"`, `items=[]`. Без этого правила бот поллит вечно.
  - `resolve` **бросил исключение** в job-loop (битый URL, недоступный cloud-api) → `status="error"`, текст исключения в top-level `error`, `items=[]`.
  - Иначе: `status="done"` если есть хоть один `done`-элемент (упавшие помечены `error` индивидуально); `status="error"` если упали все. (yadisk_batch.py:113-114 сегодня печатает ERROR и продолжает — поведение сохранено.)
- **Пер-элементный сбой:** файл из папки упал → `items[i].status="error"`, `items[i].error=...`, джоба продолжает остальные.
- API владеет очистками: tmp (`cleanup_old`), кеш (TTL 30д), staging-janitor (см. ниже) — это разные механизмы.
- **Staging/local_path:** API удаляет переданный `local_path` после завершения джобы (в `finally`, независимо от успеха/ошибки), **только если путь прошёл валидацию под `SCRIBE_STAGING_DIR`**. multipart/url стейджатся в API-владеемые workdir'ы и удаляются `cleanup_old`. Плюс **janitor**: фоновый sweep `SCRIBE_STAGING_DIR`, удаляет осиротевшие файлы старше N часов (бот упал между move и POST).
- `ELEVENLABS_API_KEY` живёт в env API, не бота.
- **Ручной end-to-end (рубеж конца Фазы 2):** запустить api.py рядом с текущим монолитом, прогнать файл + YouTube видео + Яндекс-папку против `POST /jobs`+`GET /jobs/{id}`. Это перенесённый из Фазы 1 пункт, который требовал API.

### Фаза 3 — бот → тонкий клиент
- Срезать импорты `downloader/scribe/storage`, `_archive`, `_periodic_cleanup`, всю работу с workdir/Scribe.
- **Удалить мёртвый archive-концепт:** функцию `_archive` (bot.py:195-208) и её вызов (bot.py:390), константу `ARCHIVE_DIR` + чтение `SCRIBE_ARCHIVE_DIR` (bot.py:38-39). (LOCKED: archive удалён.)
- **Оставить полный набор:** Telegram I/O, `_allowed`/allow-list, приём файла, owner-notify чужого (bot.py:217-235), `TG_FILE_LIMIT`/`TG_FILE_LIMIT_LABEL` гейт (bot.py:36-37,261-267 — Telegram-ограничение, остаётся в боте), `LOCAL_API_URL` + 30-мин session timeout (bot.py:140-147).
- **Локальный URL-sniff (заменяет `downloader.is_url`/`is_youtube_url`):** после среза импорта core `on_text` (bot.py:302) сослался бы на несуществующее имя → NameError. Решение: **бот держит собственный крошечный `URL_RE = re.compile(r"^https?://", re.IGNORECASE)`** для guard'а «это не ссылка». Классификация youtube↔direct↔yandex — НЕ дело бота, её делает `resolve.py`; бот шлёт сырой URL в `/jobs`, нераспознанный источник вернётся терминальным `error` джобы, который бот ретранслирует.
- **Передача файла из Telegram:** бот перемещает входящий файл **сразу в `SCRIBE_STAGING_DIR/<unique>`** одним `shutil.move`. `_fetch_to` (bot.py:398-405) целью даёт `SCRIBE_STAGING_DIR/<unique>`, не per-request workdir — один move, на той же ФС, что и download-каталог Bot-API (см. «Якорь staging-ФС»). На старте бот логирует WARNING при расхождении `st_dev` staging и download-каталога (иначе move тихо деградирует в copy 2 ГБ на каждую загрузку). Бот создаёт каталог (`mkdir(parents=True, exist_ok=True)`), передаёт абсолютный путь полем `local_path`. **Убрать `finally: storage.cleanup(workdir)` (bot.py:290)** — иначе бот удалит файл из-под queued-джобы. Удаление входа — на API. Имя поля — буквально `local_path`.
- **Статус → поллинг джобы:** бот поллит `GET /jobs/{id}` каждые **5 c** и редактирует ОДНО статус-сообщение. Рендер:
  - 1 элемент: эмодзи стадии из `items[0].stage` — `⬇️` (download) / `🎧` (audio) / `📝` (scribe) / `✅` (done). Сохраняет сегодняшний UX (bot.py:271/279/371).
  - N элементов: `«Обрабатываю N элементов… готово K/N»`, `K` = число `items[].status == "done"`, пересчёт на каждый poll.
  - Терминальные `{done, error}`. Общий потолок поллинга `> 6 ч` (Scribe-таймаут 6 ч, scribe.py:178) → по истечении сообщение об ошибке.
  - Транзиентный сбой GET (connection refused / 5xx — рестарт API на деплое) → **ретрай с backoff, не abort**: джоба переживает рестарт на диске.
- **Доставка N файлов (НОВАЯ логика):** по завершении бот итерирует `items[]`, шлёт один `.txt` на каждый `done`-элемент. **Тело уже в `item.text` из GET-ответа → отправлять `BufferedInputFile(item.text.encode("utf-8"), filename=f"{stem}.txt")` (in-memory, без записи на диск)** — у тонкого бота нет своего workdir/storage, временный файл некуда класть и некому чистить. **Имя файла = `<stem>.txt`** (slugified, без медиа-расширения), НЕ `item.name` (тот может нести `.mp4` — расширение на текстовом файле было бы неверным; `name` — только для отображения). По `error`-элементам — одно сообщение со списком.
- **ОБЯЗАТЕЛЬНО форвардить в `/jobs`** подсказки спикеров **и язык** (`_parse_caption` возвращает `(num, names, language)`, bot.py:127) — язык и `num_speakers`+`biased=sorted(names.values())` часть ключа кеша.

### Фаза 4 — конфиг / инфра
- `pyproject.toml`: убрать `telethon` (только arsen — проверено); добавить HTTP-сервер (fastapi/uvicorn). `requests` ОСТАЁТСЯ (core/scribe.py). `aiohttp` ОСТАЁТСЯ прямым депом — бот использует его как API-клиент; НЕ убирать как «только транзитивный».
- `uv.lock`: регенерить атомарно (`uv lock`), закоммитить.
- `.env.*`: убрать `TELEGRAM_API_ID/HASH` (arsen), `DEEPGRAM_API_KEY` (мёртвый, 0 ссылок) и `TELEGRAM_LOCAL` (орфан в .env.prod — но только после подтверждения, что он не конфигурит download-каталог bot-api, см. «Якорь staging-ФС»); добавить `SCRIBE_API_URL` боту; `ELEVENLABS_API_KEY` → читает API.
  - `SCRIBE_API_URL`: дефолт `http://127.0.0.1:8080` либо required, консистентно с тем, как `ELEVEN_KEY` читается на bot.py:30.
- **Env API-сервиса:** API читает тот же `.env.prod` (`APP_ENV=prod`) — НЕ заводим `.env.api`. `.env.prod` gitignored, на VPS попадает **только через rsync** → текущий exclude `--exclude='.env'` `--exclude='.env.local'` **не трогает `.env.prod`** (он синкается, это нужно). НЕ добавлять `.env.prod` в exclude. Health-check должен падать, если API не прочитал `ELEVENLABS_API_KEY`.
- **cookies.txt и .env.prod доставляются на `/opt/scribe-bot` ТОЛЬКО через rsync** (оба gitignored). Обязаны остаться ВНЕ `--exclude`. Новые exclude — ровно `tmp/cache/staging/jobs` и ничего, что матчит `cookies*`/`.env.prod`. Health-check дополнительно валит катовер, если на VPS `download_youtube` не резолвит `COOKIES_PATH` в существующий файл.
- `deploy/`: добавить `scribe-api.service`:
  - `ExecStart=/root/.local/bin/uv run --project /opt/scribe-bot uvicorn api:app --host 127.0.0.1 --port 8080` (**явные host/port обязательны** — uvicorn по умолчанию слушает :8000, а health-loop и `SCRIBE_API_URL` стучатся в :8080; без `--port 8080` деплой падает всегда). `WorkingDirectory=/opt/scribe-bot`, `Environment=APP_ENV=prod`, `Restart=on-failure`.
  - `scribe-bot.service` остаётся `python bot.py`, добавить `After=scribe-api.service` + `Wants=scribe-api.service`.
- **deploy.sh — полный, исполнимый порядок (сегодня deploy.sh — git push + rsync + один `uv sync` + `restart scribe-bot`, без api, без unit-инсталла, без health-gate):**
  - **Модель доставки — rsync, не git pull.** Исправить устаревший заголовок-комментарий (deploy.sh:2 «via git pull» — неверно, доставка идёт rsync'ом). `git push origin main` — оставить как опциональный архивный шаг (gitignored cookies/.env.prod/runtime НЕ едут через git, и arsen/yadisk untracked — push для VPS-дерева ничего не значит); rsync — единственный источник правды для дерева на VPS. Допустимо вынести push в отдельную строку с комментарием «archival, не доставка».
  - **Шаги строго по порядку:**
    1. `git push origin main` (опционально, архив).
    2. `rsync -av <excludes> "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"` — код (api.py + усохший bot.py) и зависимости-манифест ложатся ПЕРВЫМИ.
    3. `ssh "$HOST" "cd $REMOTE_DIR && uv sync"` — новые депы (fastapi/uvicorn) встают ДО старта api.
    4. **Установить/обновить unit-файлы:** rsync/scp `deploy/scribe-api.service` и обновлённый `deploy/scribe-bot.service` в `/etc/systemd/system/`, затем `systemctl daemon-reload` (+ на первом деплое `systemctl enable scribe-api`). Без daemon-reload новый `After=/Wants=` у бота не вступит в силу, а `systemctl restart scribe-api` на первом деплое упадёт под `set -e` (unit ещё не установлен).
    5. `ssh "$HOST" systemctl restart scribe-api`.
    6. **bounded health-loop:** `ok=""; for i in $(seq 1 "$HEALTH_RETRIES"); do ssh "$HOST" 'curl -fsS http://127.0.0.1:8080/health' && ok=1 && break; sleep "$HEALTH_INTERVAL_SEC"; done` — connection-refused = retry (uvicorn поднимается); `[ "${ok:-}" = 1 ]` иначе **abort до бота**.
    7. Health не 200 после бюджета (или 200, но key-less → не-200) — **бота не трогать** (остаётся на старом коде).
    8. `ssh "$HOST" systemctl restart scribe-bot` — только после успешного health.
  - api+bot — одно дерево, одна единица деплоя (rsync кладёт обе атомарно).
  - **rsync excludes:** добавить **все четыре** runtime-каталога — `tmp`, `cache`, `staging`, `jobs` (deploy.sh:14 сейчас исключает только `.venv/__pycache__/tmp/.git/.env/.env.local`). `rsync -av` идёт **без `--delete`** → реальный риск не удаление, а **overlay**: локальные `tmp/cache/staging/jobs` затрут одноимённые в проде (dev-овский/протухший `status.json`, который поллит бот). Exclude'ы — именно то, что не даёт этого. **`--delete` НЕ добавлять** (он снёс бы in-flight прод-джобы). Пути exclude совпадают с `SCRIBE_*_DIR`.
- `.gitignore`: добавить `cache/`, `staging/`, `jobs/`; **удалить `archive/`** (концепт удалён, не оставлять как «сосед»).
- **Документы — синхронизировать SPEC.md с принятыми решениями:**
  - SPEC.md:40,52 — `items[]` shape: `items: [ { name, text, status, stage, error } ]` (включая `stage` — это поле, по которому бот рисует single-item прогресс; без него SPEC↔PLAN расходятся). Примечание: элемент папки/плейлиста может упасть индивидуально при джобе `done`.
  - SPEC.md:51 — `POST /jobs` входы полным набором: `{url | local_path | multipart, language?, num_speakers?, speaker_names?}`. `local_path` — основной путь бота (один хост); `num_speakers` — отдельное именованное поле (часть ключа кеша).
  - SPEC.md:54 — оговорить, что `local_path`-доставка требует общей ФС у бота и API для staging-каталога (`SCRIBE_STAGING_DIR`, move а не copy); назвать его согласованной точкой handoff, чтобы сайт (Фаза 5) и любой будущий хост держали инвариант «без перезаливки».
  - SPEC.md:48 — заскоупить «без оплаты» на шаг Scribe; download+ffmpeg всё равно выполняются (URL не кешируется).
  - SPEC.md:45 — кеш хранит **только сырой Scribe JSON** как первоисточник; `.txt`/`.srt` пересобираются на чтении (НЕ «храним все три формата»).
  - SPEC.md:46 — точный состав ключа: `sha256(opus) + язык + число спикеров + biased_keywords`; display-имена спикеров — на рендере, в ключ НЕ входят.
  - `README.md`: переписать под API + клиенты.

### Фаза 5 — сайт (позже)
Второй клиент API, симметричен боту.

## Стратегия катовера (прод живой)
- Запустить `api.py` рядом с текущим монолитным ботом первым (у API нет зависимости от бота), прогнать вручную (golden-file Фазы 1 + end-to-end конца Фазы 2).
- Сохранить старый монолитный bot.py деплоябельным как откат. Опц. флаг `USE_API=1`, чтобы bot.py работал в старом режиме, пока API не доверяют.
- Определить откат `deploy.sh`/systemd, если job-модель сбоит в проде.

## Охранные правила последовательности
1. `core/` + `api.py` должны существовать ДО того, как бот срежет импорты — иначе у бота нет пайплайна.
2. Сначала вынести arsen (Фаза 0), ПОТОМ убирать `telethon` / `TELEGRAM_API_ID` (Фаза 4).
3. **yadisk_batch держит единственную логику листинга папки Яндекса** (yadisk_batch.py:43-55) — портировать в resolve.py ДО удаления. Удаление — в конце Фазы 1.
4. **cookies.txt и tmp-root: чинить пути при переносе в `core/`** (Фаза 1), иначе `__file__.parent` тихо ломает YouTube-cookies и относит tmp/ мимо deploy-exclude.
5. `ELEVENLABS_API_KEY` читается на импорте бота — убрать его использование в боте тем же изменением, что и из env бота. API читает его на старте (health-check охраняет катовер).
6. Кеш по ключу заменяет старый skip/checkpoint — отдельная skip-логика не нужна.
7. **rsync excludes (`tmp/cache/staging/jobs`) добавить тем же изменением, что вводит каталоги** — иначе первый деплой НАЛОЖИТ локальные runtime-каталоги поверх прода. `--delete` НЕ добавлять. `archive/` из `.gitignore` удалить (концепт мёртв).
8. **Деплой api+bot атомарен в строгом порядке:** rsync кода → `uv sync` → установка unit-файлов + `daemon-reload` (+ `enable scribe-api` на первом деплое) → restart scribe-api → bounded health-loop → 200 → restart scribe-bot. Пропуск инсталла unit'ов/daemon-reload = `restart scribe-api` падает под `set -e` на первом деплое, либо `After/Wants` бота тихо не активны. Health-loop: connection-refused = retry, не-200 после подъёма = abort без рестарта бота.
9. **Бот срезает импорт core, но держит локальный `URL_RE`** (Фаза 3) — иначе `on_text` (bot.py:302) даёт NameError или льёт не-URL текст в API.
10. **`local_path` от клиента валидируется под `SCRIBE_STAGING_DIR`** перед использованием и удалением — иначе клиент удалит/прочитает произвольный путь на хосте.
11. **Каждый kind терминируется в `extract_audio` (opus 32k mono), opus_hash = sha256 этого файла** — download_youtube отдаёт quality-0 stereo opus как промежуточный `src`; без extract_audio и качество Scribe падает, и hash нестабилен.
12. **uvicorn биндить явно на 127.0.0.1:8080** в unit-файле — совпасть с `SCRIBE_API_URL` и health-curl, иначе деплой падает на health-loop.
13. **Яндекс-дискриминатор = наличие `_embedded`** (dir→cloud-api для всех файлов включая 1-file folder, file→direct/yt-dlp); папка из одного файла остаётся на cloud-api ради стабильности opus-hash.

## Критерии готовности
- В `bot.py` ноль импортов `core/` пайплайна; бот держит только локальный `URL_RE`-sniff, зовёт API, шлёт `.txt` (`BufferedInputFile`, имя `<stem>.txt`) **по каждому** `done`-элементу `items[]`. Ни `_archive`, ни `ARCHIVE_DIR`, ни `SCRIBE_ARCHIVE_DIR` не осталось.
- Один API: `POST /jobs` + `GET /jobs/{id}` + `GET /health`; ссылка на папку/плейлист → N транскриптов; пер-элементный сбой не валит всю джобу; пустой/нераспознанный resolve → джоба `error`, не вечный поллинг.
- Повтор того же аудио (`file`/`youtube`/`yandex`-папка) отдаётся из кеша без оплаты Scribe (`sha256(opus)` + param_hash); смена display-имён кеш не инвалидирует, смена `biased`/языка/числа спикеров — инвалидирует; `get()` отбраковывает протухшее по mtime и продлевает TTL на хите; `direct`-хит — best-effort.
- Одиночная Яндекс-ссылка (file) идёт `download_direct` (yt-dlp); папка (dir, даже из 1 файла) — cloud-api → N элементов.
- Golden-file: рендер txt/srt байт-в-байт идентичен до/после переноса. cookies.txt находится после переноса и резолвится на VPS.
- Job-store на диске: рестарт API не сиротит поллируемую джобу; status.json пишется атомарно одним писателем, GET ретраит чтение; бот ретраит GET с backoff.
- Каждый элемент: `download_*`→`extract_audio` (моно 32k), opus_hash от выхода extract_audio; `items/<index>.txt` пишется атомарно до флипа в `done`.
- Бот рендерит прогресс: 1 элемент — эмодзи стадии из `items[].stage`; N — счётчик `готово K/N`.
- `local_path` вне `SCRIBE_STAGING_DIR` → 400; API удаляет только staging-пути; staging и download-каталог bot-api на одной ФС (иначе бот логирует WARNING на старте).
- Деплой атомарен и полон: rsync кода → uv sync → unit-инсталл + daemon-reload → restart api → bounded health-loop (200) → restart bot; uvicorn слушает 127.0.0.1:8080; rsync без `--delete` не накладывает `tmp/cache/staging/jobs`; cookies.txt/.env.prod не в exclude; заголовок deploy.sh говорит «via rsync».
- SPEC.md синхронизирован: `items[].{status,stage,error}`, полный POST-контракт с именованными language/num_speakers/local_path + same-FS примечание к local_path, scope «без оплаты», кеш = только raw JSON, ключ без display-имён.
- arsen вне репозитория (заморожен); нет мёртвых зависимостей и ключей (`telethon`, `DEEPGRAM_API_KEY`, `TELEGRAM_API_ID/HASH`, `TELEGRAM_LOCAL`).

## Отложенная полировка (Low — решается на этапе реализации)
Аудит подтвердил readiness без этих пунктов; вложить по ходу Фазы 1-4:
- Janitor sweep age N for orphaned staging files is left as 'N hours' — pick a concrete default (e.g. reuse cleanup_old's 6h or a dedicated STAGING_TTL_HOURS env) so the two cleanup mechanisms don't silently diverge. Low.
- The same-FS st_dev check needs the bot-api download dir path to be discoverable at bot startup; plan anchors it to /opt/scribe-bot/botapi 'documented near the bot-api unit file' but that unit lives outside this repo. Add an explicit BOTAPI_DOWNLOAD_DIR env (default /opt/scribe-bot/botapi) the bot reads, rather than relying on out-of-repo documentation, so the WARNING check has a concrete path source. Low/medium.
- opus_hash field is written into status.json items but never read by the GET contract or the bot; it's effectively debug/provenance only. Harmless, but worth a one-line note that it is not part of the external API shape so SPEC items[] stays {name,text,status,stage,error} without opus_hash. Low.
- Cache param key uses biased=sorted(speaker_names.values()) but scribe.transcribe sends biased_keywords as json.dumps(list) in original insertion order (scribe.py:169); since the key sorts and the request doesn't, two caption orderings of the same names hit the same cache entry but could have produced different Scribe byte output if ElevenLabs is order-sensitive to biased_keywords. Almost certainly order-insensitive, so sorting for the key is the right dedup choice, but worth a one-line acknowledgement. Low.
- USE_API=1 rollback flag (катовер strategy) implies bot.py keeps BOTH the old monolithic pipeline AND the thin-client path during cutover, which contradicts Phase 3's 'срезать импорты core'. Acceptable as a temporary transitional state, but the plan should note the import-cut is the END state and the flag is a short-lived bridge, so an implementer doesn't try to do both in one commit. Low.
