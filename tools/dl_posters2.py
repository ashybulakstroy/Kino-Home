import json, os, re, sys
sys.path.insert(0, '.')
import generate_page as gp

data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))
session = gp.SESSION
changed = 0

for t in data:
    pu = t.get('poster_url', '') or ''
    if pu and os.path.exists(pu):
        continue
    tid = t['topic_id']
    cache_path = os.path.join(gp.TOPIC_CACHE_DIR, f'{tid}.html')
    if not os.path.exists(cache_path):
        print(f'{tid}: no cache, skip')
        continue
    html = open(cache_path, 'rb').read().decode('windows-1251', errors='replace')
    info = gp.parse_topic_for_magnet(html)
    poster_url = info.get('poster', '')
    if poster_url:
        local = gp.download_rutracker_poster(tid, poster_url)
        if local and os.path.exists(local):
            t['poster_url'] = local
            changed += 1
            print(f'{tid}: {poster_url[:50]}... -> {local}')
        else:
            print(f'{tid}: found {poster_url[:50]}... but download failed')
    else:
        print(f'{tid}: no poster in cached topic page')

if changed:
    json.dump(data, open('torrents_data.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    html = gp.generate_html(data)
    open('index-kino.html', 'w', encoding='utf-8').write(html)
    print(f'OK, {changed} posters updated')
else:
    print('No changes')
