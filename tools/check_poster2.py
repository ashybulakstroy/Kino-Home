import json, os
data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))
for t in data:
    pu = t.get('poster_url', '') or ''
    exists = os.path.exists(pu) if pu else False
    if not pu or not exists:
        print(f'{t["topic_id"]}: poster_url={pu!r} exists={exists}')
