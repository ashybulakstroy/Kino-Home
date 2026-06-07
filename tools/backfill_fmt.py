import json, sys, time
sys.path.insert(0, '.')
import generate_page as gp

data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))
changed = 0
for t in data:
    if t.get('format'):
        continue
    print('Fetching', t['topic_id'], '...', end=' ', flush=True)
    try:
        html = gp.fetch(t['topic_url'], timeout=10)
        info = gp.parse_topic_for_magnet(html)
        if info.get('format'):
            t['format'] = info['format']
            changed += 1
            print('->', info['format'])
        else:
            print('-> no format')
    except Exception as e:
        print('-> error:', e)
    time.sleep(1.5)

if changed:
    json.dump(data, open('torrents_data.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    html = gp.generate_html(data)
    open('index-kino.html', 'w', encoding='utf-8').write(html)
    print('OK, format added to', changed, 'topics')
else:
    print('No changes')
