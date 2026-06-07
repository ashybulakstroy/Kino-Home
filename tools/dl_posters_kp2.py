import json, os, sys, re
sys.path.insert(0, '.')
import generate_page as gp

session = gp.SESSION
data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))

# init KP session
session.get('https://www.kinopoisk.ru/', timeout=10)

for t in data:
    pu = t.get('poster_url', '') or ''
    if pu and os.path.exists(pu):
        continue
    kp_id = t.get('kp_id')
    if not kp_id:
        continue
    tid = t['topic_id']
    url = f'https://www.kinopoisk.ru/film/{kp_id}/'
    try:
        r = session.get(url, timeout=15)
        html = r.text
        if len(html) < 5000:
            # retry with main page visit
            session.get('https://www.kinopoisk.ru/', timeout=10)
            r = session.get(url, timeout=15)
            html = r.text
        m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if m:
            poster_url = m.group(1)
            print(f'{tid}: {poster_url[:60]}...', end=' ', flush=True)
            local = gp.download_rutracker_poster(tid, poster_url)
            if local and os.path.exists(local):
                t['poster_url'] = local
                print('OK')
            else:
                print('download failed')
        else:
            print(f'{tid}: no og:image (len={len(html)})')
            # also check for avatar images
            avatars = re.findall(r'(https://avatars\.mds\.yandex[^"\']+)', html)
            if avatars:
                print(f'  avatar: {avatars[0][:60]}')
    except Exception as e:
        print(f'{tid}: error: {e}')

if any(t.get('poster_url') and os.path.exists(t.get('poster_url','')) for t in data):
    json.dump(data, open('torrents_data.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    html = gp.generate_html(data)
    open('index-kino.html', 'w', encoding='utf-8').write(html)
    print('Saved')
