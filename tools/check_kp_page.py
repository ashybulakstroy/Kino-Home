import requests, re
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
r = session.get('https://www.kinopoisk.ru/film/5452841/', timeout=15)
html = r.text
# look for any image link with kinopoisk
for m in re.finditer(r'<meta[^>]*image[^>]*content="([^"]+)"', html, re.I):
    print(f'meta image: {m.group(1)}')
for m in re.finditer(r'(https://avatars\.mds\.yandex[^"\']+)', html):
    print(f'avatar: {m.group(1)[:80]}')
# check if there's a poster link
for m in re.finditer(r'class="[^"]*film-poster[^"]*"[^>]*href="([^"]+)"', html):
    print(f'film-poster href: {m.group(1)}')
for m in re.finditer(r'class="[^"]*poster[^"]*"[^>]*src="([^"]+)"', html):
    print(f'poster src: {m.group(1)[:80]}')
