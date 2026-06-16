"""Clear BOTH cache and topic data, then re-enrich with NEW scoring."""

import json, sys, time, urllib.request

DATA = 'data/torrents_data.json'
CACHE = 'data/youtube_cache.json'
API = 'http://localhost:14876'

# Films with wrong trailers (topic_id → film title for display)
WRONG = {
    '6807509': 'Детство, которого не было',
    '5122768': 'Mandariinid',
    '6628186': 'Nähtamatuvõitlus',
    '1722250': 'Satybaldy Narymbetov)',
    '6488101': 'Птицы без гнёзд',
    '6816085': 'Ogon',
    '6523153': 'Skhema',
    '3988422': 'Racketeer',
    '6655401': 'Опознание',
    '6844432': 'Родной очаг',
    '689107': 'Rusuli samkudhedi',
    '6705292': 'Dala qasqiri',
    '6807409': 'Тимбилдинг',
    '6868155': 'Бродячий автобус',
    '6376417': 'Америкэн бой',
    '2168': 'Тестовый',
}

# Load data
d = json.load(open(DATA, 'r', encoding='utf-8'))
cache = json.load(open(CACHE, 'r', encoding='utf-8'))

# Build topic lookup
by_tid = {str(t.get('topic_id', '')): t for t in d}

# Build cache keys to clear
# cache key format is "{orig_title}|{year}".lower()
cache_keys_to_clear = set()
cleared_topics = []

for tid, name in WRONG.items():
    t = by_tid.get(tid)
    if not t:
        continue
    title = t.get('orig_title') or t['movie_title']
    year = t['movie_year']
    cache_key = f'{title}|{year}'.lower()
    
    # Clear data
    old_url = str(t.get('youtube_url', ''))[:60]
    t['youtube_url'] = None
    cache_keys_to_clear.add(cache_key)
    cleared_topics.append(tid)
    print(f'#{tid} {name}: очищен (было: {old_url})')

# Also clear cache keys for the wrong video IDs
# Some films may be cached under different keys (e.g., different title normalization)
# Let's also clear keys whose VALUE matches known bad URLs
bad_urls = {
    d['youtube_url']
    for d in d
    if str(d.get('topic_id', '')) in WRONG and d.get('youtube_url')
}
# Actually we just cleared them above, need to fetch before clearing
# Re-scan: collect old URLs before clearing
d2 = json.load(open(DATA, 'r', encoding='utf-8'))
for t in d2:
    if str(t.get('topic_id', '')) in WRONG:
        url = t.get('youtube_url', '')
        if url and url not in ('0', 'None', None):
            # Find all cache keys pointing to this URL
            for k, v in cache.items():
                if v == url:
                    cache_keys_to_clear.add(k)
                    print(f'  кеш "{k}" → {url[:50]}')

# Clear cache entries
for k in cache_keys_to_clear:
    if k in cache:
        old_val = str(cache[k])[:50]
        cache[k] = None
        print(f'  кеш "{k}" очищен (было: {old_val})')

# Save both files
json.dump(d, open(DATA, 'w', encoding='utf-8', newline=''), ensure_ascii=False, indent=2)
json.dump(cache, open(CACHE, 'w', encoding='utf-8', newline=''), ensure_ascii=False, indent=2)

print(f'\nОчищено topics: {len(cleared_topics)}, кеш-ключей: {len(cache_keys_to_clear)}')

# Re-enrich
if cleared_topics:
    print('\nЗапуск enrich через API (12 шт, ~2 мин)...')
    for tid in cleared_topics:
        url = f'{API}/enrich/{tid}'
        try:
            req = urllib.request.Request(url, method='POST', data=b'')
            resp = urllib.request.urlopen(req, timeout=5)
            status = json.loads(resp.read())
            print(f'  #{tid}: {status["status"]}')
        except Exception as e:
            print(f'  #{tid}: ERROR {e}')
        time.sleep(0.2)

    print('\nОжидание...')
    for _ in range(24):  # ~2 min
        time.sleep(5)
        remaining = 0
        for tid in cleared_topics:
            try:
                req = urllib.request.Request(f'{API}/enrich/status/{tid}')
                resp = urllib.request.urlopen(req, timeout=3)
                st = json.loads(resp.read())['status']
                if st in ('in_progress', 'queued'):
                    remaining += 1
            except:
                pass
        print(f'  ... {remaining} осталось')
        if remaining == 0:
            break

    print('\nРЕЗУЛЬТАТЫ:')
    d3 = json.load(open(DATA, 'r', encoding='utf-8'))
    by_tid3 = {str(t.get('topic_id', '')): t for t in d3}
    for tid in cleared_topics:
        t = by_tid3.get(tid)
        if t:
            title = t.get('orig_title') or t['movie_title']
            yt = t.get('youtube_url')
            status = '✅' if yt and str(yt) not in ('', 'None', '0') else '❌ None'
            print(f'  {status} #{tid} {title} → {str(yt)[:70] if yt else "None"}')
