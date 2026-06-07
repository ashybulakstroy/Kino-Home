# AGENTS.md — LocaL-Kino

Домашний кинотеатр: каталог фильмов с rutracker.net (раздел f=22) + стриминг через libtorrent. Полный аналог HomeKino, но источник — rutracker вместо Pirate Bay.

## Быстрый старт

```powershell
.venv\Scripts\Activate.ps1
python generate_page.py --refresh   # первый парсинг (долгий)
python stream_server.py             # → http://localhost:8765 (порт из SERVER_PORT в .env)
```

## Команды

| Команда | Назначение |
|---------|-----------|
| `python generate_page.py` | Сгенерировать `index-kino.html` из кеша |
| `python generate_page.py --refresh` | Полный перепарсинг rutracker f=22 (~100 топиков) |
| `python generate_page.py --pages 10` | Парсинг первых N страниц (по умолчанию 10, пока не наберётся ~100 топиков) |
| `python generate_page.py --refresh --quick` | Парсинг только 1-й страницы (быстрая проверка) |
| `python generate_page.py --refresh --pages 3` | Парсинг ровно 3 страниц (игнорируя лимит топиков) |
| `python generate_page.py --refresh --collection=kino_sng` | Парсинг другой коллекции, слияние с кешем |
| `python generate_page.py --refresh --collection=kino_sng --limit 10` | Парсинг только 10 тем из коллекции |
| `python generate_page.py --collection=kino_sng` | Генерация HTML из кеша (без перепарсинга) |
| `python stream_server.py` | Запустить веб-сервер |

## Архитектура

| Файл | Назначение |
|------|-----------|
| `generate_page.py` | Парсинг rutracker f=22, обогащение IMDB + Кинопоиск, генерация `index-kino.html` |
| `engine.py` | libtorrent-движок: магнеты, приоритеты кусков, readahead |
| `stream_server.py` | Flask-сервер: стриминг, прогресс, AAC-транскодинг |
| `player.html` | Страница плеера с прогресс-баром |
| `config.py` | Чтение `.env` |

## Отличия от HomeKino (Pirate Bay)

- **Источник:** rutracker.net, раздел f=22 (Зарубежные фильмы)
- **100 страниц** по 50 тем = до 5000 раздач (по умолчанию ~100 топиков, лимит `MAX_TOPICS`)
- **Нет магнетов в списке** — парсинг двухуровневый:
  1. Спарсить `viewforum.php?f=22&start=N` (заголовки, размер, сиды)
  2. Зайти в `viewtopic.php?t=XXXXX`, извлечь magnet из `fieldset.attach a.magnet-link`
- **Rate limit:** `time.sleep(1.5)` между запросами к topic pages
- **Рейтинг:** Кинопоиск (основной) + IMDB (если найден)
- Первый полный парсинг может занять ~30 мин (100 топиков). Процесс сохраняет прогресс — можно прервать и продолжить.
- На странице Кинопоиска парсинг может блокироваться — срабатывает retry с посещением главной страницы для получения cookies

## Названия фильмов

Формат: `Оригинал (Перевод) [Год, Жанр, Качество]`

Для IMDB поиска извлекается английское название (оригинал в скобках `()` или до скобок, если там латиница).

## Зависимости

- `beautifulsoup4`, `requests` — парсинг
- `flask` — HTTP-сервер
- `libtorrent` — стриминг (требует OpenSSL 1.1 DLLs на Windows)
- `ffmpeg` / `ffprobe` — AAC-транскодинг

## Язык

- Код — английский
- UI, сообщения, README — русский
