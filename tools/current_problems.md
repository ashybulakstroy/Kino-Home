# Current Problems

## Решено: Серые/пустые постеры в browse-режимах
- **Причина**: 3 фильма без poster_url + не везде был fallback на placeholder.png.
  - JS `posterUrl()` в `/browse/random` и `/browse/filter` возвращал `''` → `background:#333`
  - Python `_poster_style()` в `/browse/carousel` и `/browse/timeline` возвращал `background:#333`
  - `/browse/shuffle`, `/browse/duel`, `/browse/matrix` уже использовали `pu()` с placeholder fallback — не трогали
- **Фикс**: 
  1. `_poster_style()` — оба `background:#333` заменены на `background-image:url(/data/posters/placeholder.png)`
  2. `posterUrl()` (random + filter) — пустая строка заменена на `'/data/posters/placeholder.png'`
- **Статус**: Исправлено, все страницы отвечают 200.

## Известные проблемы
- *(none currently)*
