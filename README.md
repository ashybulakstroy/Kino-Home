# LocaL-Kino

Домашний кинотеатр: каталог фильмов с rutracker.net и локальный стриминг через libtorrent.

## Быстрый старт

```powershell
.venv\Scripts\Activate.ps1
python generate_page.py --refresh --quick
python stream_server.py
```

Сервер открывается на порту из `.env` (`SERVER_PORT`), по умолчанию в текущем проекте это `14876`.

## Основные команды

```powershell
python generate_page.py
python generate_page.py --refresh
python generate_page.py --refresh --quick
python generate_page.py --refresh --pages 3
python generate_page.py --refresh --collection=kino_sng --limit 10
python stream_server.py
```

## Важные файлы

- `generate_page.py` - парсинг rutracker, обогащение данных, генерация `index-kino.html`.
- `engine.py` - libtorrent-движок, выбор видеофайла, приоритеты кусков, readahead.
- `stream_server.py` - Flask-сервер, стриминг, прогресс, транскодинг AAC через ffmpeg.
- `player.html` - страница плеера.
- `config.py` - чтение `.env`.
- `.env.example` - шаблон настроек.

## Рабочие данные

Эти файлы и папки генерируются локально и не попадают в git:

- `data/temp/` - временные скачанные видео.
- `data/posters/` - постеры.
- `data/topic_cache/` - кеш страниц rutracker.
- `data/*_cache.json`, `data/torrents_data.json`, `data/index-kino.html` - кеши и сгенерированный каталог.

Перед запуском сервера проверь размер `data/temp/`: при превышении `MAX_TEMP_SIZE_GB` сервер может удалить старые файлы из этой папки.

`TOPIC_MAX_AGE_DAYS` задаёт возраст тем в днях. При housekeeping сервер удаляет из каталога темы старше этого возраста, их topic-cache и неиспользуемые постеры.
