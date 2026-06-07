import json
data = json.load(open('torrents_data.json'))
hidden = {'ужасы', 'эротика', 'порно', 'для взрослых', 'взрослый', 'horror', 'erotica', 'adult'}
for t in data:
    g = (t.get('genre') or '').lower()
    for h in hidden:
        if h in g:
            print(f'{t["topic_id"]}: {t.get("movie_title","?"):40s} genre={t.get("genre","")!r}')
