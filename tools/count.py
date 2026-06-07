import re
html = open('index-kino.html', encoding='utf-8').read()
rows = len(re.findall(r'<tr[^>]*data-date', html))
tiles = len(re.findall(r'<div class="tile-card"', html))
print(f'Rows: {rows}, Tiles: {tiles}')
