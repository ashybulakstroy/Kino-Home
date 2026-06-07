import json, os, sys
sys.path.insert(0, '.')
import generate_page as gp

data = json.load(open('torrents_data.json', 'r', encoding='utf-8'))

for t in data:
    pu = t.get('poster_url', '') or ''
    if pu and os.path.exists(pu):
        continue
    tid = t['topic_id']
    print(f'\n--- {tid}: {t.get("movie_title","?")} ---')
    print(f'  imdb_id: {t.get("imdb_id","")}  kp_id: {t.get("kp_id","")}')

    # Try IMDB poster
    if t.get('imdb_id'):
        url, kp_rating = gp.fetch_imdb_rating(t['imdb_id'])
        print(f'  fetch_imdb_rating returned: {url}')
        # fetch_imdb_rating returns (poster_url, rating) for main page

    # Try KP poster
    if t.get('kp_id'):
        url, kp_rating, kp_votes = gp.fetch_kinopoisk_page(t['kp_id'])
        print(f'  fetch_kinopoisk_page returned: poster={url}, rating={kp_rating}')
