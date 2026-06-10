"""Check oldest movies' trailers against new scoring logic. Optimized."""
import json, sys, time, re
sys.path.insert(0, '.')
from generate_page import (
    search_youtube_trailer, _youtube_candidates, _score_youtube_candidate,
    HEADERS, SESSION
)
import urllib.parse

data = json.load(open('data/torrents_data.json', encoding='utf-8'))

seen = set()
candidates = []
for t in data:
    url = t.get('youtube_url', '')
    if not url or url in ('0', '', None):
        continue
    title = t.get('orig_title') or t.get('movie_title', '')
    year = t.get('movie_year', '')
    key = (title.lower().strip(), str(year))
    if key in seen or not title:
        continue
    seen.add(key)
    try:
        y = int(year)
    except:
        y = 9999
    candidates.append((y, title, year, url, t.get('kp_id'), t.get('imdb_id'), t.get('topic_id')))

candidates.sort(key=lambda x: x[0])
print(f'Movies with trailers: {len(candidates)}. Checking oldest 25...\n')

results = []

for y, title, year_str, current_url, kp, imdb, tid in candidates[:25]:
    vid_match = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', current_url)
    if not vid_match:
        results.append((y, title, year_str, current_url, -99, None, tid, 'invalid url'))
        continue
    current_vid = vid_match.group(1)

    # Quick HEAD to see if video exists
    exists = True
    try:
        r = SESSION.head(f'https://www.youtube.com/watch?v={current_vid}', timeout=8)
        if r.status_code >= 400:
            exists = False
    except:
        exists = False

    if not exists:
        results.append((y, title, year_str, current_url, -99, None, tid, 'NOT_FOUND'))
        print(f'  ❌ {year_str} | {title} — ВИДЕО НЕ СУЩЕСТВУЕТ')
        continue

    # Run new search once; also score current video from same search results
    best_new_url = None
    current_score = -99
    query = f'{title} {year_str} trailer'
    url = f'https://www.youtube.com/results?search_query={urllib.parse.quote(query)}'
    try:
        r = SESSION.get(url, timeout=10)
        best_cand = None
        for c in _youtube_candidates(r.text):
            s = _score_youtube_candidate(c, title, year_str)
            if c['video_id'] == current_vid:
                current_score = s
            if not best_cand or s > best_cand['score']:
                best_cand = {**c, 'score': s}
        if best_cand and best_cand['score'] >= 20:
            best_new_url = f"https://www.youtube.com/watch?v={best_cand['video_id']}"
    except:
        pass

    is_same = best_new_url and current_vid in best_new_url
    is_suspicious = current_score < 40 or (best_new_url and not is_same)

    if is_suspicious:
        results.append((y, title, year_str, current_url, current_score, best_new_url, tid, f'score={current_score}'))
        icon = '⚠️'
    else:
        icon = '✅'

    new_info = best_new_url[:75] if best_new_url else 'None'
    print(f'  {icon} {year_str} | {title}')
    print(f'       cur: {current_url[:70]} score={current_score}')
    print(f'       new: {new_info}')

    time.sleep(0.3)

# Results
results.sort(key=lambda x: (x[4] if isinstance(x[4], (int,float)) else 0))
print(f'\n\n========== ТОП-10 ПОДОЗРИТЕЛЬНЫХ ТРЕЙЛЕРОВ ==========\n')
for i, (y, title, year_str, cur, score, new_url, tid, reason) in enumerate(results[:10], 1):
    print(f'{i}. {year_str} | {title} (tid={tid})')
    print(f'   Причина: {reason}')
    print(f'   Сейчас:   {cur}')
    if new_url and '/results?' not in new_url:
        print(f'   Кандидат: {new_url}')
    else:
        print(f'   Кандидат: не найден')
    print()
