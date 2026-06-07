import json, os, re
import generate_page as gp

data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))
changed = 0
for t in data:
    if t.get('format'):
        continue
    cache_path = os.path.join(gp.TOPIC_CACHE_DIR, t['topic_id'] + '.html')
    if not os.path.exists(cache_path):
        print(t['topic_id'], 'no cache')
        continue
    html = open(cache_path, 'r', encoding='utf-8').read()
    info = gp.parse_topic_for_magnet(html)
    if info.get('format'):
        t['format'] = info['format']
        changed += 1
        print(t['topic_id'], '->', info['format'])
    else:
        print(t['topic_id'], '-> no format in cache')

if changed:
    json.dump(data, open('torrents_data.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    html = gp.generate_html(data)
    open('index-kino.html', 'w', encoding='utf-8').write(html)
    print('OK, format added to', changed, 'topics')
else:
    print('No changes')
