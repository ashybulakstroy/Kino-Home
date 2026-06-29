"""Локальный поиск по rutracker через pagination viewforum.php.
Собирает заголовки тем со страниц форумов в data/forum_index.json."""

import json
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config import DATA_DIR, WORKER_COUNT
import generate_page as gp


INDEX_FILE = DATA_DIR / 'forum_index.json'

MOVIE_FORUMS = [
    ('22', 'Наше кино'),
    ('2540', 'Кино СНГ'),
    ('1247', 'Кино СНГ HD'),
    ('252', 'Новинки 2026'),
]

PAGE_SIZE = 50


def _fetch_forum_page(fid, start):
    """Fetch one page of a viewforum.php listing."""
    url = f'https://rutracker.net/forum/viewforum.php?f={fid}&start={start}'
    r = gp.SESSION.get(url, timeout=15)
    if r.status_code != 200:
        return None
    return r.content.decode('cp1251', errors='replace')


def _load_existing_index():
    """Load existing forum index from disk, or return empty."""
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text('utf-8'))
        except (OSError, json.JSONDecodeError):
            pass
    return {'meta': {}, 'topics': {}}


def _make_entry(t, fid, fname):
    tid = str(t.get('topic_id') or '')
    if not tid:
        return None
    return (tid, {
        'topic_id': tid,
        'title': t.get('title', ''),
        'movie_title': t.get('movie_title', '') or t.get('title', ''),
        'forum_id': fid,
        'forum_name': fname,
        'seeders': t.get('seeders', 0),
        'size_str': t.get('size_str', ''),
    })


def build_index(max_pages_per_forum=50, update_mode=False, refresh_pages=5,
                forums=None):
    """Build or update data/forum_index.json.

    Args:
        max_pages_per_forum: pages to scan on full build.
        update_mode: if True, only scan first refresh_pages and merge with existing.
        refresh_pages: pages to scan in update mode (default 5).
        forums: list of (fid, fname) to process; None means all.

    Returns number of topics in the index after build.
    """
    if forums is None:
        forums = MOVIE_FORUMS
    if update_mode:
        existing = _load_existing_index()
        all_topics = existing.get('topics', {})
        pages_to_scan = min(refresh_pages, max_pages_per_forum)
        mode_label = f'update (first {pages_to_scan} pages, merge)'
    else:
        all_topics = {}
        pages_to_scan = max_pages_per_forum
        mode_label = f'full build ({pages_to_scan} pages)'

    print(f'  {mode_label}')
    new_count = 0
    for fid, fname in forums:
        print(f'  scanning {fname} (f={fid}) ...')
        batch_size = max(1, WORKER_COUNT)
        for batch_start in range(0, pages_to_scan, batch_size):
            batch_end = min(batch_start + batch_size, pages_to_scan)
            batch_pages = list(range(batch_start, batch_end))
            page_results = {}
            with ThreadPoolExecutor(max_workers=batch_size) as ex:
                fut = {ex.submit(_fetch_forum_page, fid, p * PAGE_SIZE): p for p in batch_pages}
                for f in as_completed(fut):
                    p = fut[f]
                    try:
                        html = f.result()
                    except Exception:
                        html = None
                    if html:
                        page_results[p] = gp.parse_forum_page(html, collection='rutracker_search')
            for page in batch_pages:
                topics = page_results.get(page)
                if topics is None:
                    continue
                if not topics:
                    break
                for t in topics:
                    entry = _make_entry(t, fid, fname)
                    if entry:
                        tid, data = entry
                        if tid not in all_topics:
                            all_topics[tid] = data
                            new_count += 1
            count_so_far = len([k for k in all_topics if all_topics[k]['forum_id'] == fid])
            new_in_batch = sum(1 for p in batch_pages if p in page_results)
            print(f'    pages {batch_start+1}-{batch_end}: {new_in_batch} pages, {count_so_far} topics')
        forum_count = len([t for t in all_topics.values() if t['forum_id'] == fid])
        print(f'  done: {forum_count} topics from {fname}')

    if update_mode:
        old_total = existing.get('meta', {}).get('total_topics', 0)
        print(f'  merged: {old_total} old + {new_count} new = {len(all_topics)} total')
    else:
        new_count = len(all_topics)

    data = {
        'meta': {
            'built_at': gp.now_text(),
            'total_topics': len(all_topics),
            'pages_scanned': pages_to_scan,
            'update_mode': update_mode,
        },
        'topics': all_topics,
    }
    INDEX_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    print(f'index saved: {len(all_topics)} topics in {INDEX_FILE}')
    return len(all_topics)


def search(query, limit=30):
    """Search the forum index by title substring match."""
    if not INDEX_FILE.exists():
        return []
    q = query.lower().strip()
    if not q:
        return []
    data = json.loads(INDEX_FILE.read_text('utf-8'))
    topics = data.get('topics', {})
    results = []
    for tid, t in topics.items():
        title = (t.get('title') or '').lower()
        movie_title = (t.get('movie_title') or '').lower()
        if q in title or q in movie_title:
            results.append({
                'topic_id': tid,
                'title': t.get('title', ''),
                'movie_title': t.get('movie_title', ''),
                'forum_id': t.get('forum_id', ''),
                'forum_name': t.get('forum_name', ''),
                'seeders': t.get('seeders', 0),
                'size_str': t.get('size_str', ''),
            })
            if len(results) >= limit:
                break
    return results


def index_exists():
    """Check if forum index has been built."""
    return INDEX_FILE.exists()


def index_stats():
    """Return metadata about the built index."""
    if not INDEX_FILE.exists():
        return None
    data = json.loads(INDEX_FILE.read_text('utf-8'))
    meta = data.get('meta', {})
    meta['index_path'] = str(INDEX_FILE)
    return meta


def search_results_as_topic_cards(results):
    """Convert search results to the topic-card format for discover API."""
    cards = []
    for r in results:
        topic_url = f'https://rutracker.net/forum/viewtopic.php?t={r["topic_id"]}'
        cards.append({
            'status': 'Найдено в индексе RuTracker',
            'source': f'RuTracker — {r.get("forum_name", "")}',
            'topic_id': r['topic_id'],
            'title': r.get('title', ''),
            'orig_title': '',
            'year': '',
            'seeders': r.get('seeders', 0),
            'size': r.get('size_str', ''),
            'poster_url': '/data/posters/placeholder.png',
            'has_poster': False,
            'has_magnet': False,
            'magnet': '',
            'topic_url': topic_url,
            'imdb_id': '',
            'kp_id': '',
        })
    return cards


if __name__ == '__main__':
    import sys
    raw = [a for a in sys.argv[1:] if a]
    update = '--refresh' in raw or '-r' in raw
    pages = 5 if update else 50
    fid_filter = None
    for a in raw:
        if a.isdigit():
            pages = int(a)
        elif a.startswith('--collection='):
            val = a.split('=', 1)[1]
            sel = [f for f in MOVIE_FORUMS if f[0] == val]
            if sel:
                fid_filter = sel
    mode = 'update' if update else 'full build'
    scope = f'forum={fid_filter[0][0]}' if fid_filter else 'all forums'
    print(f'Forum index {mode} (max {pages} pages, {scope}) ...')
    total = build_index(max_pages_per_forum=pages, update_mode=update,
                        refresh_pages=pages, forums=fid_filter)
    print(f'Done: {total} topics indexed')
