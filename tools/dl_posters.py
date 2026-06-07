import json, os, requests, sys
sys.path.insert(0, '.')
import generate_page as gp

data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))
session = requests.Session()
session.headers.update(gp.HEADERS)

for t in data:
    pu = t.get('poster_url', '') or ''
    if pu and not os.path.exists(pu):
        tid = t['topic_id']
        local_path = f"{gp.POSTERS_DIR}/{tid}.jpg"
        print(f'{tid}: {pu[:60]}...', end=' ', flush=True)
        try:
            r = session.get(pu, timeout=30)
            if r.status_code == 200:
                os.makedirs(gp.POSTERS_DIR, exist_ok=True)
                with open(local_path, 'wb') as f:
                    f.write(r.content)
                t['poster_url'] = local_path
                print(f'OK -> {local_path}')
            else:
                print(f'HTTP {r.status_code}')
        except Exception as e:
            print(f'error: {e}')

json.dump(data, open('torrents_data.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
html = gp.generate_html(data)
open('index-kino.html', 'w', encoding='utf-8').write(html)
print('Saved')
