import json
data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))
missing = [t for t in data if not t.get('poster_url')]
failed = [t for t in data if t.get('_magnet_failed')]
print('Без постера:', len(missing))
print('_magnet_failed:', len(failed))
for t in missing[:5]:
    print(f'  {t["topic_id"]}: {t.get("movie_title","?")} failed={t.get("_magnet_failed")}')
for t in failed[:5]:
    print(f'  failed: {t["topic_id"]}: {t.get("movie_title","?")}')
