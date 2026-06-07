import json
data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))
has = [t for t in data if t.get('format')]
print('Topics with format:', len(has), '/', len(data))
for t in has[:15]:
    print(' ', t['topic_id'], ':', t['format'])
if not has:
    print('No format found in any topic')
    # Check what HTML pages contain
    import os, re
    for fn in os.listdir('topic_cache')[:5]:
        html = open(os.path.join('topic_cache', fn), 'r', encoding='utf-8').read()
        # look for format
        for m in re.finditer(r'Формат[^<]*', html):
            print('  ', fn, '->', m.group())
