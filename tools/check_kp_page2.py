import requests, re
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
r = session.get('https://www.kinopoisk.ru/film/5452841/', timeout=15)
print(f'Status: {r.status_code}')
print(f'Len: {len(r.text)}')
print(f'URL: {r.url}')
# print first 2000 chars
print(r.text[:2000])
