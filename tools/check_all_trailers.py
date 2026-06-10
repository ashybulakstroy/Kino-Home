"""Check movies with trailers, oldest first. Shows logs. Saves at end."""
import json, sys, time, re, os
sys.path.insert(0, '.')
from generate_page import (
    _youtube_candidates, _score_youtube_candidate,
    YOUTUBE_CACHE, load_json, save_json, HEADERS, SESSION
)
import urllib.parse

DATA_FILE = 'data/torrents_data.json'
data = json.load(open(DATA_FILE, encoding='utf-8'))

with_trailer = []
for t in data:
    url = t.get('youtube_url', '')
    if not url or url in ('0', '', None): continue
    title = t.get('orig_title') or t.get('movie_title', '')
    year = t.get('movie_year', '')
    if not title: continue
    try: y = int(year)
    except: y = 9999
    with_trailer.append((y, title, str(year), url, t.get('topic_id')))

with_trailer.sort(key=lambda x: x[0])
# Limit to movies before 2025 (old ones more likely broken)
with_trailer = [x for x in with_trailer if x[0] < 2025]

total = len(with_trailer)
print(f'Проверяю {total} фильмов (до 2025 года), от самых старых...\n')

replaced = 0
ok_count = 0
dead = 0

for idx, (y, title, year_str, current_url, tid) in enumerate(with_trailer, 1):
    print(f'═══════════════════════════════════════════════')
    print(f'[{idx}/{total}] {title} ({year_str})')
    print(f'Тема: https://rutracker.net/forum/viewtopic.php?t={tid}')
    print(f'Текущий трейлер: {current_url}')

    vid_match = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', current_url)
    if not vid_match:
        print('❌ некорректный URL\n')
        dead += 1
        continue
    vid = vid_match.group(1)

    # HEAD check
    try:
        r = SESSION.head(f'https://www.youtube.com/watch?v={vid}', timeout=8)
        if r.status_code >= 400:
            print(f'❌ ВИДЕО НЕДОСТУПНО (HTTP {r.status_code})\n')
            dead += 1
            continue
    except:
        print(f'❌ ВИДЕО НЕДОСТУПНО\n')
        dead += 1
        continue

    # Search
    queries = [
        f'{title} {year_str} official trailer',
        f'{title} {year_str} трейлер',
        f'"{title}" {year_str} трейлер фильм',
        f'{title} {year_str} trailer',
        f'{title} official trailer',
        f'{title} трейлер',
    ]
    best = None
    current_score = -99
    for q in queries:
        url = f'https://www.youtube.com/results?search_query={urllib.parse.quote(q)}'
        try:
            r = SESSION.get(url, timeout=10)
            for c in _youtube_candidates(r.text):
                s = _score_youtube_candidate(c, title, year_str)
                if c['video_id'] == vid:
                    current_score = s
                if not best or s > best['score']:
                    best = {**c, 'score': s}
        except:
            pass
        print('.', end='', flush=True)
        time.sleep(0.1)
    print()

    score_str = f'score={current_score}' if current_score > -99 else '❌ НЕ В ВЫДАЧЕ'
    print(f'Текущее ({vid}): {score_str}')

    if best and best['video_id'] != vid and best['score'] >= 40:
        new_url = f"https://www.youtube.com/watch?v={best['video_id']}"
        print(f'🔴 ЗАМЕНА: {best["score"]} -> {new_url}')
        print(f'   Название: {best["title"][:70]}')
        # Apply to data
        for t in data:
            if t.get('topic_id') == tid:
                t['youtube_url'] = new_url
                break
        # Update cache
        yt_cache = load_json(YOUTUBE_CACHE) or {}
        yt_cache[f'{title}|{year_str}'.lower()] = new_url
        save_json(YOUTUBE_CACHE, yt_cache)
        replaced += 1
    elif best and best['video_id'] == vid:
        print(f'🟢 OK (score={best["score"]})')
        ok_count += 1
    else:
        print(f'🟡 НЕТ АЛЬТЕРНАТИВЫ')
        ok_count += 1
    print()

# Final save
json.dump(data, open(DATA_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'═══════════════════════════════════════════════')
print(f'ГОТОВО!')
print(f'  Всего проверено: {total}')
print(f'  Заменено: {replaced}')
print(f'  Оставлено (OK без замены): {ok_count}')
print(f'  Недоступно: {dead}')
