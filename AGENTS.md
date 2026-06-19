# AGENTS.md — LocaL-Kino

Домашний кинотеатр: каталог фильмов с rutracker.net + локальный стриминг через libtorrent. Источник данных — rutracker, данные проекта хранятся в `data/`.

## Быстрый старт

```powershell
.venv\Scripts\Activate.ps1
python generate_page.py --refresh --quick
python stream_server.py             # -> http://localhost:14876, если SERVER_PORT=14876
```

Порт берётся из `.env` (`SERVER_PORT`). Если переменная не задана, код использует значение по умолчанию из `config.py`.

## Рабочее правило

Перед отчётом пользователю, что задача сделана, нужно сначала проверить результат: компиляцией, HTTP-проверкой, чтением данных, логами или другим подходящим способом по смыслу изменения.

Если был добавлен или изменён код, нельзя сообщать о готовности, пока не сделан смоук-тест именно затронутого сценария и не получено доказательство, что поведение работает как требовалось. В отчёте нужно кратко указать, чем именно проверено.

## Снапшоты

Папка для снапшотов проекта: `C:\Backup\Work\`.

## Команды

| Команда | Назначение |
|---------|------------|
| `python generate_page.py` | Сгенерировать `index-kino.html` из кеша |
| `python generate_page.py --refresh` | Обновить все коллекции |
| `python generate_page.py --refresh --fast` | Быстрый refresh без тяжёлого обогащения рейтингов/трейлеров |
| `python generate_page.py --refresh --quick` | Быстрый refresh первой страницы |
| `python generate_page.py --refresh --collection=kino_sng` | Обновить одну коллекцию |
| `python generate_page.py --refresh --collection=kino_sng --limit 10` | Добавить до 10 новых тем из одной коллекции |
| `python generate_page.py --collection=kino_sng` | Сгенерировать HTML из кеша одной коллекции |
| `python stream_server.py` | Запустить веб-сервер |

## Архитектура

| Файл | Назначение |
|------|------------|
| `generate_page.py` | Коллекции, парсинг rutracker, magnet, постеры, рейтинги, генерация каталога, санитизация запрещённых тем |
| `stream_server.py` | Flask-сервер, стриминг, browse-страницы, авто-refresh, housekeeping, плеер |
| `engine.py` | libtorrent-движок: magnet, выбор видеофайла, приоритеты кусков, readahead |
| `player.html` | Страница плеера |
| `config.py` | Чтение `.env` |
| `project_io.py` | Пути `data/`, блокировки, атомарная запись JSON |

## Коллекции

Коллекции задаются в `generate_page.py`:

| ID | Название | URL |
|----|----------|-----|
| `nashe_kino` | Наше кино | `https://rutracker.net/forum/viewforum.php?f=22` |
| `kino_sng` | Фильмы ближнего зарубежья | `https://rutracker.net/forum/viewforum.php?f=2540` |
| `novinki_2026` | Новинки 2026 | `https://rutracker.net/forum/viewforum.php?f=252` |
| `kino_sng_hd` | Фильмы Ближнего Зарубежья (HD Video) | `https://rutracker.net/forum/viewforum.php?f=1247` |
| `piratebay_top` | World TOP | `https://1.piratebays.to/top/207` |

`MAX_TOPICS=30` — лимит пополнения коллекции за один refresh. Это не жёсткий лимит размера коллекции. Если в коллекции уже больше 30 тем, они остаются валидными, пока не удалены housekeeping.

Темы без magnet не нужны для приложения и не должны попадать в итоговый каталог.

## Парсинг rutracker

Парсинг двухуровневый:

1. Читать `viewforum.php` и брать список тем.
2. Открывать `viewtopic.php?t=XXXXX`, извлекать magnet, постер, дату регистрации темы и дополнительные признаки.

Коллекция `piratebay_top` использует отдельный одноуровневый парсер Pirate Bay: magnet берётся сразу из listing, `topic_id` формируется как `pb_<btih>`, постеры и рейтинги догоняются обычным enrich.

Refresh должен сливать новые темы с существующим кешем, а не обрезать коллекцию до последних 30. Если новых тем нет, тяжёлое обогащение можно пропускать.

Кеш листинга форума `data/topic_cache/f*_p*.html` не должен скрывать новые темы. При refresh сначала скачивается свежий `viewforum.php`; локальный listing-cache используется только как fallback при сетевой ошибке и только если он не старше `LISTING_CACHE_MAX_AGE_DAYS` из `.env` (по умолчанию 1 день).

Pipeline при `--refresh`:
1. Парсинг страниц форума
2. Слияние с кешем (только новые темы)
3. Загрузка magnet и постеров с rutracker
4. Поиск IMDB ID
5. Поиск Кинопоиск рейтинга
6. Загрузка IMDB ratings/basics
7. Обогащение (жанры, рейтинги, трейлеры, IMDB-постеры, KP-постеры для новых)
8. Санитизация запрещённых тем
9. Кинопоиск постеры для всех тем (шаг 10) — закрывает старые темы без постеров
10. Генерация HTML

## Авто-refresh

Серверный авто-refresh общий для всех коллекций.

Логика:

1. После старта сервер выполняет housekeeping.
2. Затем проверяет `data/last_refresh_date.txt`.
3. Если файла нет или дата внутри отличается от текущей даты, запускается refresh.
4. После успешного refresh в файл записывается только дата без времени.
5. Дальше сервер повторяет проверку примерно каждые 12 часов.

Чтобы сервис работал во время обновления, refresh выполняется через staging-папку `data/staging_refresh`, после чего готовые файлы переносятся в основную `data/`.

Серверный авто-refresh запускается в быстром режиме `--fast`: новые темы получают magnet/poster, а тяжёлое обогащение рейтингов, трейлеров и дополнительных постеров пропускается. Полное обогащение запускается ручным `python generate_page.py --refresh` или админским `/refresh`.

Параллельные сетевые операции используют общий параметр `.env` `WORKER_COUNT` (сейчас `8`): загрузка magnet/poster, ручная очередь enrich и фоновый enrich должны брать одно и то же значение.

## Public Mode

`PUBLIC_MODE=1` в `.env` предназначен для публикации наружу. В этом режиме Flask не раздаёт корень проекта как static; доступны только явно разрешённые файлы (`/`, `/player.html`, `/data/posters/*`) и browse-страницы. Админские endpoints `/refresh`, `/cleanup`, `/hide`, `/unhide`, `/enrich/*` отключены. `/watch` и `/watch_sync` принимают только magnet, чей info-hash уже есть в `data/torrents_data.json`.

## Housekeeping

`TOPIC_MAX_AGE_DAYS` задаётся в `.env`, по умолчанию `90`.

Возрастная чистка применяется по коллекциям:

- `nashe_kino` — чистить старые темы;
- `novinki_2026` — чистить старые темы;
- `kino_sng` — не чистить по возрасту;
- `kino_sng_hd` — не чистить по возрасту.

Старые темы удаляются только если коллекция остаётся больше `MAX_TOPICS`. Удалённые темы архивируются в `data/topics_archive.json`. Вместе с ними чистятся связанные данные: `data/topic_cache/` и неиспользуемые постеры.

Если у темы нет даты регистрации в системе, её нужно заполнить текущей датой.

## Санитизация запрещённых тем

`FORBIDDEN_GENRES` в `generate_page.py` (`ужасы/horror/секс/sex/эротика/erotica/порно/porn`):

- Проверка на этапе `generate_html()`: темы с запрещёнными жанрами обнуляются (imdb_id=0, kp_id=0, рейтинги=0, magnet=0, poster=placeholder) и добавляются в `hidden_topics.json`.
- Скрытые темы не отображаются ни в каталоге, ни в browse-режимах.
- API: `POST /hide/<topic_id>`, `POST /unhide/<topic_id>`.
- `data/hidden_topics.json` — список скрытых topic_id.

## Browse-страницы

В `stream_server.py` есть режимы (все на отдельных `/browse/*` URL):

| Маршрут | Режим | Описание |
|---------|-------|----------|
| `/` | Корневая | Каталог `index-kino.html` с панелью ссылок на browse-режимы |
| `/test` | Тестовая | Список всех browse-режимов с карточками и ссылками |
| `/browse/carousel` | Карусель | Горизонтальная карусель постеров |
| `/browse/random` | Случайный | Один случайный фильм на весь экран с кнопкой «Дальше» |
| `/browse/filter` | Фильтр | Фильтр по жанрам, годам, рейтингу, коллекции, формату (MKV/MP4/AVI/MPEG), сидам |
| `/browse/timeline` | Хронология | Фильмы по годам (сетка сгруппированных постеров) |
| `/browse/shuffle` | ТВ / Киноплёнка | Слайд-шоу на весь экран: плёнка скользит справа налево, постер по центру. Кнопки 5с/10с/15с/30с, пауза |
| `/browse/duel` | Дуэль | Два фильма VS, клик по постеру — победа + следующий раунд, счётчик |
| `/browse/matrix` | Матрица | 4×2 вертикальных постера (aspect-ratio 2:3), автообновление каждые 10с, пауза/пуск, ручной шафл (Fisher-Yates), клик → плеер |
| `/browse/stats` | Статистика | Топ-15 жанров, распределение по годам (canvas-график), по коллекциям, общее количество, средний рейтинг |
| `/browse/search` | Поиск | Поиск по названию (русскому и оригинальному), фильтрация в реальном времени, сетка постеров |
| `/browse/top` | Топ | Топ-50 фильмов по рейтингу Кинопоиска и IMDB, номера мест, бейджи рейтинга |
| `/browse/collections` | Коллекции | Группировка по коллекциям (Наше кино, Новинки, Кино СНГ, Кино СНГ HD), аккордеон |

После изменения browse-страниц нужно проверять, что сервер стартует и что страницы открываются без ошибок:
- Для `/browse/shuffle` — placeholder-постеры и JavaScript (анимация постера).
- Для `/browse/matrix` — aspect-ratio, data-hash, делегирование onclick.
- Для `/browse/filter` — формат-фильтр (toUpperCase, бейдж).
- Для `/browse/stats` — canvas-график годов.

## Постеры

Постеры сохраняются локально в `data/posters/`.

Источники постера (в порядке попытки):

1. **rutracker.net** — URL из `title` атрибута `var.postImgAligned.img-right`. Скачивается как `{topic_id}.jpg` через `download_rutracker_poster()`.
2. **IMDB** — из поиска по названию (`search_imdb_ids`) или со страницы рейтинга (`og:image`). Скачивается как `{imdb_id}.jpg` через `download_poster()`.
3. **Кинопоиск** — из CDN `st.kp.yandex.net/images/film_big/{kp_id}.jpg`. Скачивается как `kp_{kp_id}.jpg` через `download_kinopoisk_poster()`. Запускается в двух местах:
   - В `enrich()` — для новых тем после обогащения
   - В `main()` — отдельный проход по ВСЕМ темам (шаг 10), чтобы закрыть старые темы без постеров

Если все источники недоступны, используется локальная заглушка:

```text
data/posters/placeholder.png
```

Во всех browse-режимах (`pu()`, `posterUrl()`, `_poster_style()`), при отсутствии постера или его файла, возвращается `placeholder.png` вместо пустоты/серого фона.

Если меняется формат заглушки, нужно синхронно обновить все ссылки в `generate_page.py`, `stream_server.py`, HTML и data.

## Стриминг

`engine.py` отвечает за libtorrent и приоритеты загрузки. `stream_server.py` отдаёт видео и может использовать ffmpeg/ffprobe для проверки и транскодинга.

Если пользователь выбирает файл, который плохо подходит для live-просмотра, сервер может искать более подходящую раздачу того же фильма по нормализованному названию, году, коллекции, magnet, размеру и количеству сидов. Пользовательское сообщение должно быть простым: найден быстрый способ live-просмотра, без технического перечисления контейнеров.

## Известные баги и фиксы

### has_real_poster — проверка соответствия IMDB

`generate_page.py:has_real_poster()` проверял только существование файла. Если у темы менялся `imdb_id` (например, PirateBay detail-страница дала правильный ID), а `poster_url` указывал на старый `ttXXXXXXX.jpg`, то `has_real_poster` возвращал True и enrich не перекачивал постер.

**Фикс**: если `poster_url` содержит `ttXXXXXXX` — проверять, что этот ID совпадает с текущим `imdb_id` темы.

```python
imdb_id = topic.get('imdb_id', '')
if imdb_id and re.search(r'/tt\d+', poster_url):
    m = re.search(r'tt(\d+)', poster_url)
    if m and m.group(0) != imdb_id:
        return False
```

### enrich_topic — fetch_imdb_rating не вызывался при listing-category genre

`generate_page.py:2446`: условие было `(needs_genre and not topic.get('genre')) or not topic.get('imdb_rating')`. Когда жанр — категория листинга (не пустая, но бесполезная, например "спорт" с PirateBay), `not topic.get('genre')` давал False, и `fetch_imdb_rating` не вызывался. IMDB-постер (из `og:image`) не скачивался.

**Фикс**: `if needs_genre or not topic.get('imdb_rating'):`

### KP cache poisoning — search_kinopoisk возвращал неправильные ID

`search_kinopoisk()` принимал `title` как есть — с тех-мусором из PirateBay (BONE, DDP5.1, WEB-DL, x265...). Кинопоиск не находил такой запрос и возвращал первый случайный фильм. Результат кешировался в `kp_search_cache.json` и использовался для всех последующих запросов с тем же названием.

**Симптомы**: у 30+ PirateBay-тем был одинаковый `kp_id=6606844` (фильм "Давление").

**Фиксы**:

1. `_clean_kp_search_title(title)` — удаляет технические маркеры из заголовка перед поиском: 1080p, 4K, WEB-DL, BONE, DDP5, x265, HEVC и т.д.

2. `_kp_verify_result(title, year, rating_html)` — после получения KP ID проверяет `og:title` страницы фильма: год в скобках должен совпадать с искомым, хотя бы одно слово (длиннее 2 символов) из названия должно присутствовать. Если проверка не проходит — возвращает None, неправильный результат не кешируется.

3. В `enrich_topic()` для PirateBay-тем (`pb_*`) принудительно перезапрашивается KP ID (даже если `kp_rating` уже установлен). Если KP ID изменился — очищается `poster_url` и `_poster_failed_at` для перекачки.

4. В `stream_server.py:_topic_enrich_needs()` добавлен `kp_due` — PirateBay-темы с невалидным или отсутствующим KP ID после валидации включаются в enrich для повторной проверки.

### _kp_validated / _kp_retried флаги

Для PirateBay-тем после попытки найти KP ID устанавливается `_kp_validated = True`. Если KP ID не найден — дополнительно `_kp_retried = True`, и снимается `_poster_failed_at`, чтобы не блокировать скачку постера от других источников (IMDB).

Без этого тема помечалась как "готовая" на 7 дней (`POSTER_RETRY_DAYS`), даже если улучшенный поиск мог бы найти правильный фильм.

## API endpoints

| Маршрут | Метод | Описание |
|---------|-------|----------|
| `/watch` | POST | Добавить magnet в сессию |
| `/start` | POST | Запустить сессию |
| `/watch_sync` | POST | Добавить magnet + дождаться готовности |
| `/status/<info_hash>` | GET | Статус загрузки |
| `/stream/<info_hash>` | GET | Потоковое видео |
| `/transcode/<info_hash>` | GET | Транскодинг через ffmpeg |
| `/stop_session` | POST | Остановить сессию |
| `/refresh` | GET | Запустить refresh всех коллекций |
| `/cleanup` | GET | Очистка старых файлов |
| `/hide/<topic_id>` | POST | Скрыть тему |
| `/unhide/<topic_id>` | POST | Отобразить скрытую тему |
| `/enrich/all` | POST | Обогатить все темы |
| `/enrich/<topic_id>` | POST | Обогатить одну тему |

## Рабочие данные

Основные локальные данные:

- `data/torrents_data.json` — каталог.
- `data/posters/` — постеры.
- `data/topic_cache/` — кеш тем.
- `data/imdb/` — полные локальные IMDB справочники `title.ratings.tsv.gz` и `title.basics.tsv.gz`; JSON-кеши хранят выжимку по найденным ID.
- `data/temp/` — временные видеофайлы.
- `data/staging_refresh/` — staging во время refresh.
- `data/topics_archive.json` — архив старых удалённых тем.
- `data/last_refresh_date.txt` — дата последнего успешного авто-refresh.
- `data/hidden_topics.json` — скрытые или исключённые темы.
- `data/index-kino.html` — сгенерированная HTML-страница.

Перед запуском сервера полезно проверить `data/temp/`: при превышении `MAX_TEMP_SIZE_GB` housekeeping может удалить старые временные файлы.

## Зависимости

- `beautifulsoup4`, `requests` — парсинг.
- `flask` — HTTP-сервер.
- `libtorrent` — стриминг.
- `ffmpeg` / `ffprobe` — транскодинг и проверка медиа.

## Язык

- Код — английский.
- UI, сообщения, README и AGENTS — русский.
