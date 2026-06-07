import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import generate_page as gp
data = json.load(open('torrents_data.json'))
print(f'Всего: {len(data)}')
for t in data:
    topic_id = t['topic_id']
    genre = gp.clean_and_translate_genre(t.get('genre', ''))
    if genre != t.get('genre', ''):
        t['genre'] = genre
    print(f'{topic_id}: {t.get("movie_title","?"):40s} genre={t.get("genre","")!r}')
json.dump(data, open('torrents_data.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
html = gp.generate_html(data)
open('index-kino.html', 'w', encoding='utf-8').write(html)
print('Saved OK')
