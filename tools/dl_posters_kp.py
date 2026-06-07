import json, os, sys, re, requests
sys.path.insert(0, '.')
import generate_page as gp

data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))
session = gp.SESSION
session.headers.update(gp.HEADERS)

for t in data:
    pu = t.get('poster_url', '') or ''
    if pu and os.path.exists(pu):
        continue
    kp_id = t.get('kp_id')
    if not kp_id:
        continue
    tid = t['topic_id']
    print(f'{tid}: kp_id={kp_id}', end=' ', flush=True)
    url = f'https://www.kinopoisk.ru/film/{kp_id}/'
    try:
        r = session.get(url, timeout=15)
        html = r.text
        m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if m:
            poster_url = m.group(1)
            print(f'poster: {poster_url[:60]}...')
            local = gp.download_rutracker_poster(tid, poster_url)
            if local and os.path.exists(local):
                t['poster_url'] = local
                print(f'  -> saved {local}')
            else:
                print(f'  -> download failed')
        else:
            print('no og:image found')
    except Exception as e:
        print(f'error: {e}')

if any(t.get('poster_url') and os.path.exists(t.get('poster_url','')) for t in data):
    json.dump(data, open('torrents_data.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    html = gp.generate_html(data)
    open('index-kino.html', 'w', encoding='utf-8').write(html)
    print('Saved')
