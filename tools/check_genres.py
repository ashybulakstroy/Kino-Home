import json
data = json.load(open('torrents_data.json'))
for t in data:
    print(f'{t["topic_id"]}: genre={t.get("genre","")!r}')
