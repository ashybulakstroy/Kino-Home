import requests, re
s = requests.Session()
s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
r = s.get('https://www.kinopoisk.ru/film/5452841/', timeout=15)
print(f'Status: {r.status_code}, URL: {r.url}')
print(f'Len: {len(r.text)}')
m = re.search(r'<h1[^>]*>(.*?)</h1>', r.text, re.DOTALL)
if m: 
    import html
    print(f'H1: {html.unescape(m.group(1))[:200]}')
