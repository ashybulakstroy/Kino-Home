"""Validate all existing youtube_url against new scoring logic (via oEmbed)."""

import json, sys, time

sys.path.insert(0, '.')
from generate_page import _score_youtube_candidate, SESSION

DATA = 'data/torrents_data.json'
MIN_SCORE = 35

d = json.load(open(DATA, 'r', encoding='utf-8'))

with_yt = [
    t for t in d
    if t.get('youtube_url') and '/results?' not in str(t.get('youtube_url', ''))
    and t.get('youtube_url') not in ('0', '')
]

print(f'Total topics: {len(d)}')
print(f'Topics with real youtube_url: {len(with_yt)}')
sys.stdout.flush()

suspicious = []
ok_count = 0
fetch_errors = 0

for i, t in enumerate(with_yt, 1):
    vid = t['youtube_url']
    vid_id = vid.split('v=')[-1].split('&')[0]

    oembed_url = (
        f'https://www.youtube.com/oembed?'
        f'url=https://www.youtube.com/watch?v={vid_id}&format=json'
    )
    try:
        resp = SESSION.get(oembed_url, timeout=5)
        if resp.status_code != 200:
            raise Exception(f'HTTP {resp.status_code}')
        info = resp.json()
        cand_title = info.get('title', '')
        cand_channel = info.get('author_name', '')
    except Exception as e:
        err = str(e)[:60]
        print(f'[{i}/{len(with_yt)}] #{t["topic_id"]} — FETCH ERROR: {err}')
        sys.stdout.flush()
        fetch_errors += 1
        time.sleep(0.2)
        continue

    candidate = {
        'title': cand_title,
        'channel': cand_channel,
        'video_id': vid_id,
        'length': '2:00',
    }

    title = t.get('orig_title') or t['movie_title']
    year = t['movie_year']
    score = _score_youtube_candidate(candidate, title, year)

    if score >= MIN_SCORE:
        ok_count += 1
        print(f'[{i}/{len(with_yt)}] #{t["topic_id"]} {title} ({year}) — {score} [OK]')
    else:
        suspicious.append((t, candidate, score))
        print(f'[{i}/{len(with_yt)}] #{t["topic_id"]} {title} ({year}) — {score} [SUSPICIOUS]')
        print(f'    видео: {cand_title[:100]}')
        print(f'    канал: {cand_channel}')
        print(f'    URL:   {vid}')

    sys.stdout.flush()
    time.sleep(0.25)

print()
print('=' * 60)
print(f'RESULTS: OK={ok_count}  SUSPICIOUS={len(suspicious)}  FETCH_ERRORS={fetch_errors}')
print('=' * 60)
if suspicious:
    print()
    for t, cand, score in suspicious:
        title = t.get('orig_title') or t['movie_title']
        print(f'#{t["topic_id"]} {title} ({t["movie_year"]}) — score={score}')
        print(f'  URL:   {t["youtube_url"]}')
        print(f'  Видео: {cand["title"][:100]}')
        print(f'  Канал: {cand["channel"]}')
        print()
