import hashlib
import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from html import unescape

from bs4 import BeautifulSoup


WORLD_SOURCES = {"piratebay", "tpbparty"}
WORLD_COLLECTIONS = {"piratebay_top", "tpbparty_top"}


def is_world_source(source):
    return str(source or "").strip().lower() in WORLD_SOURCES


def is_world_collection(collection):
    return str(collection or "").strip().lower() in WORLD_COLLECTIONS


def is_world_topic(topic):
    return is_world_source(topic.get("source")) or is_world_collection(topic.get("collection"))


def filter_world_top(topics: list[dict], max_display: int = 60) -> list[dict]:
    """For each world collection, keep only the top N topics by seeders.
    Non-world topics pass through unchanged.
    Returns the filtered list in the same relative order."""
    world = []
    other = []
    for t in topics:
        if is_world_collection(t.get('collection')):
            world.append(t)
        else:
            other.append(t)

    by_collection: dict[str, list[dict]] = {}
    for t in world:
        coll = t.get('collection', '')
        by_collection.setdefault(coll, []).append(t)

    for coll in by_collection:
        by_collection[coll].sort(key=lambda x: -(x.get('seeders') or 0))
        removed = len(by_collection[coll]) - max_display
        if removed > 0:
            print(f"  {coll}: обрезано {removed} тем (оставлено топ-{max_display} по сидам)")
        by_collection[coll] = by_collection[coll][:max_display]

    result = other[:]
    for coll in by_collection:
        result.extend(by_collection[coll])
    return result


def parse_world_date(raw):
    raw = (raw or "").strip()
    now = datetime.now(timezone.utc)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", raw)
    if m:
        try:
            return datetime(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                int(m.group(5)),
                tzinfo=timezone.utc,
            ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return now.strftime("%Y-%m-%d %H:%M")
    m = re.match(r"^(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", raw)
    if m:
        try:
            return datetime(
                now.year,
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                tzinfo=timezone.utc,
            ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return now.strftime("%Y-%m-%d %H:%M")
    m = re.match(r"^(?:Today|Сегодня)\s+(\d{1,2}):(\d{2})", raw, re.I)
    if m:
        return now.replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
        ).strftime("%Y-%m-%d %H:%M")
    m = re.match(r"^(?:Y[- ]?day|Yesterday|Вчера)\s+(\d{1,2}):(\d{2})", raw, re.I)
    if m:
        return (now - timedelta(days=1)).replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
        ).strftime("%Y-%m-%d %H:%M")
    for unit, delta_arg in (
        ("min|mins|minute|minutes", "minutes"),
        ("hour|hours", "hours"),
        ("day|days", "days"),
        ("week|weeks", "weeks"),
    ):
        m = re.match(rf"(\d+)\s+({unit})", raw, re.I)
        if m:
            return (now - timedelta(**{delta_arg: int(m.group(1))})).strftime("%Y-%m-%d %H:%M")
    return now.strftime("%Y-%m-%d %H:%M")


def _parse_int_text(value):
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else 0


def world_topic_id(prefix, magnet, info_hash_from_magnet):
    info_hash = info_hash_from_magnet(magnet)
    return f"{prefix}_{info_hash}" if info_hash else ""


def parse_world_page(
    html,
    collection,
    source,
    clean_title,
    parse_size,
    now_text,
    info_hash_from_magnet,
    detect_format_from_text,
):
    if source == "piratebay":
        return parse_piratebay_page(
            html,
            collection,
            clean_title,
            parse_size,
            now_text,
            info_hash_from_magnet,
            detect_format_from_text,
        )
    if source == "tpbparty":
        return parse_tpbparty_page(
            html,
            collection,
            clean_title,
            parse_size,
            now_text,
            info_hash_from_magnet,
            detect_format_from_text,
        )
    return []


def parse_piratebay_page(
    html,
    collection,
    clean_title,
    parse_size,
    now_text,
    info_hash_from_magnet,
    detect_format_from_text,
):
    soup = BeautifulSoup(html, "html.parser")
    topics = []
    rows = soup.select("#searchResult tbody tr")
    for row in rows:
        title_el = row.select_one("a.detLink")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        magnet_el = row.select_one('a[href^="magnet:"]')
        magnet = magnet_el.get("href", "") if magnet_el else ""
        topic_id = world_topic_id("pb", magnet, info_hash_from_magnet)
        if not topic_id:
            continue
        tds = row.find_all("td")
        category_el = row.select_one(".vertTh a")
        category = category_el.get_text(strip=True) if category_el else ""
        uploaded = tds[2].get_text(strip=True) if len(tds) > 2 else ""
        size_str = tds[4].get_text(strip=True) if len(tds) > 4 else ""
        seeders = tds[5].get_text(strip=True) if len(tds) > 5 else "0"
        leechers = tds[6].get_text(strip=True) if len(tds) > 6 else "0"
        author_el = tds[7].select_one("a") if len(tds) > 7 else None
        author = author_el.get_text(strip=True) if author_el else ""
        href = title_el.get("href") or ""
        topic_url = urllib.parse.urljoin("https://1.piratebays.to", href)
        movie_title, movie_year = clean_title(title)
        size_bytes, size_clean = parse_size(size_str)
        topics.append({
            "topic_id": topic_id,
            "title": title,
            "movie_title": movie_title,
            "orig_title": "",
            "movie_year": movie_year,
            "genre": "",
            "quality": "",
            "collection": collection,
            "source": "piratebay",
            "source_category": category,
            "author": author,
            "size_str": size_clean,
            "size_bytes": size_bytes,
            "seeders": _parse_int_text(seeders),
            "leechers": _parse_int_text(leechers),
            "date_str": parse_world_date(uploaded),
            "added_at": now_text(),
            "topic_url": topic_url,
            "listing_order": len(topics),
            "magnet": magnet,
            "imdb_id": None,
            "imdb_rating": None,
            "imdb_votes": None,
            "kp_id": None,
            "kp_rating": None,
            "kp_votes": None,
            "poster_url": "",
            "cast": "",
            "youtube_url": None,
            "format": detect_format_from_text(title),
        })
    return topics


def parse_tpbparty_page(
    html,
    collection,
    clean_title,
    parse_size,
    now_text,
    info_hash_from_magnet,
    detect_format_from_text,
):
    soup = BeautifulSoup(html, "html.parser")
    topics = []
    rows = soup.select("#searchResult > tr:not(.header):not(.altHeader)")
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 8:
            continue
        title_el = tds[1].find("a") if len(tds) > 1 else None
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        magnet_el = row.select_one('a[href^="magnet:"]')
        magnet = magnet_el.get("href", "") if magnet_el else ""
        topic_id = world_topic_id("tpb", magnet, info_hash_from_magnet)
        if not topic_id:
            continue
        category_el = row.select_one(".vertTh a")
        category = category_el.get_text(strip=True) if category_el else ""
        href = title_el.get("href", "")
        topic_url = urllib.parse.urljoin("https://tpb.party", href) if href else ""
        uploaded = tds[2].get_text(strip=True) if len(tds) > 2 else ""
        size_str = tds[4].get_text(strip=True) if len(tds) > 4 else ""
        seeders = tds[5].get_text(strip=True) if len(tds) > 5 else "0"
        leechers = tds[6].get_text(strip=True) if len(tds) > 6 else "0"
        author_el = tds[7].find("a") if len(tds) > 7 else None
        author = author_el.get_text(strip=True) if author_el else "Anonymous"
        movie_title, movie_year = clean_title(title)
        size_bytes, size_clean = parse_size(size_str)
        topics.append({
            "topic_id": topic_id,
            "title": title,
            "movie_title": movie_title,
            "orig_title": "",
            "movie_year": movie_year,
            "genre": "",
            "quality": "",
            "collection": collection,
            "source": "tpbparty",
            "source_category": category,
            "author": author,
            "size_str": size_clean,
            "size_bytes": size_bytes,
            "seeders": _parse_int_text(seeders),
            "leechers": _parse_int_text(leechers),
            "date_str": parse_world_date(uploaded),
            "added_at": now_text(),
            "topic_url": topic_url,
            "listing_order": len(topics),
            "magnet": magnet,
            "imdb_id": None,
            "imdb_rating": None,
            "imdb_votes": None,
            "kp_id": None,
            "kp_rating": None,
            "kp_votes": None,
            "poster_url": "",
            "cast": "",
            "youtube_url": None,
            "format": detect_format_from_text(title),
        })
    return topics


def _levenshtein(a, b):
    m, n = len(a), len(b)
    if m < n:
        a, b = b, a
        m, n = n, m
    prev = list(range(n + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(curr[-1] + 1, prev[j + 1] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[n]


def _enrich_field_empty(t, field):
    val = t.get(field)
    if val is None:
        return True
    if isinstance(val, str) and val in ('', '0'):
        return True
    if isinstance(val, (int, float)) and val == 0:
        return True
    return False


def deduplicate_world_topics(topics):
    seen_exact = set()
    seen_fuzzy = []
    result = []
    for topic in topics:
        title = str(topic.get("movie_title") or "").lower().strip()
        year = str(topic.get("movie_year") or "")
        if not title:
            continue
        key = (title, year)
        if key in seen_exact:
            continue
        seen_exact.add(key)
        is_fuzzy_duplicate = False
        for fuzzy_title, fuzzy_year in seen_fuzzy:
            if fuzzy_year == year and _levenshtein(title, fuzzy_title) <= 2:
                is_fuzzy_duplicate = True
                break
        if is_fuzzy_duplicate:
            continue
        seen_fuzzy.append((title, year))
        result.append(topic)
    return result


_ENRICH_FIELDS = {
    'imdb_id', 'kp_id',
    'imdb_rating', 'imdb_votes',
    'kp_rating', 'kp_votes',
    'poster_url',
    'youtube_url',
    'genre',
    'movie_title', 'orig_title', 'movie_year',
    'cast',
    '_kp_validated', '_kp_retried', '_poster_failed_at',
}


def merge_world_duplicates(topics):
    """Merge duplicate world topics, keeping the one with most seeders.

    For each fuzzy-duplicate group (movie_title + movie_year):
    - Pick the topic with highest seeders as the primary
    - Preserve enrich fields (ratings, poster, trailer, ids, genre) from
      any group member
    - Listing fields (magnet, size, format, seeders, etc.) come from primary
    - Non-world topics pass through unchanged
    """
    world = []
    non_world = []
    for t in topics:
        if is_world_topic(t):
            world.append(t)
        else:
            non_world.append(t)

    if not world:
        return topics

    groups = []
    used = set()
    for i, t in enumerate(world):
        if i in used:
            continue
        title = str(t.get("movie_title") or "").lower().strip()
        year = str(t.get("movie_year") or "")
        if not title:
            non_world.append(t)
            continue
        group = [t]
        used.add(i)
        for j, u in enumerate(world):
            if j in used:
                continue
            u_title = str(u.get("movie_title") or "").lower().strip()
            u_year = str(u.get("movie_year") or "")
            if not u_title:
                continue
            if u_year == year and _levenshtein(title, u_title) <= 2:
                group.append(u)
                used.add(j)
        groups.append(group)

    result = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
            continue

        def _seeders(t):
            s = t.get('seeders')
            try:
                return int(s) if s is not None else -1
            except (ValueError, TypeError):
                return -1

        def _has_magnet(t):
            return bool(t.get('magnet'))

        best_candidates = [t for t in group if _has_magnet(t)]
        if best_candidates:
            best = max(best_candidates, key=_seeders)
        else:
            best = max(group, key=_seeders)

        for field in _ENRICH_FIELDS:
            if _enrich_field_empty(best, field):
                for member in group:
                    if member is not best and not _enrich_field_empty(member, field):
                        best[field] = member[field]
                        break

        result.append(best)

    return non_world + result


def world_page_hash(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#searchResult")
    content = table.get_text("\n", strip=True) if table else (html or "")
    return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()


def normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def title_tokens(title):
    return {token for token in re.findall(r"[a-z0-9]+", normalize_text(title)) if len(token) >= 3}


def youtube_video_id(url):
    if not url:
        return ""
    match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
    return match.group(1) if match else ""


def youtube_oembed(session, video_url):
    oembed_url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(video_url, safe="")
    response = session.get(oembed_url, timeout=8)
    if response.status_code != 200:
        return None
    return response.json()


def score_world_trailer_candidate(meta, movie_title, movie_year):
    video_title = normalize_text(unescape(meta.get("title", "")))
    author = normalize_text(meta.get("author_name", ""))
    tokens = title_tokens(movie_title)
    if not tokens or not all(token in video_title for token in tokens):
        return 0
    bad_words = (
        "review", "reaction", "explained", "ending", "clip", "scene", "song",
        "soundtrack", "interview", "behind the scenes", "news", "real paramount",
    )
    if any(word in video_title for word in bad_words):
        return 0
    score = len(tokens) * 2
    if "official trailer" in video_title:
        score += 8
    elif "trailer" in video_title:
        score += 5
    else:
        return 0
    if movie_year and str(movie_year) in video_title:
        score += 2
    author_words = set(author.split())
    if author_words & {"distribution", "pictures", "studios", "films"}:
        score += 5
    elif author_words & {"trailer", "trailers", "media"}:
        score += 1
    return score


def is_verified_world_youtube_trailer(session, url, title, year):
    try:
        meta = youtube_oembed(session, url)
        return bool(meta and score_world_trailer_candidate(meta, title, year) > 0)
    except Exception:
        return False


def search_world_youtube_trailer(session, title, year):
    queries = [
        f"{title} official trailer",
        f"{title} official trailer {year}",
        f"{title} {year} official trailer",
        f"{title} trailer",
    ]
    try:
        ids = []
        for query in queries:
            url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
            response = session.get(url, timeout=10)
            for pattern in (r"/watch\?v=([a-zA-Z0-9_-]{11})", r'"videoId":"([a-zA-Z0-9_-]{11})"'):
                for video_id in re.findall(pattern, response.text):
                    if video_id not in ids:
                        ids.append(video_id)
                    if len(ids) >= 24:
                        break
                if len(ids) >= 24:
                    break
            if len(ids) >= 24:
                break
        best_url = None
        best_score = 0
        for video_id in ids:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                meta = youtube_oembed(session, video_url)
            except Exception:
                continue
            if not meta:
                continue
            score = score_world_trailer_candidate(meta, title, year)
            if score > best_score:
                best_url = video_url
                best_score = score
        if best_url:
            return best_url
    except Exception:
        pass
    return None


# Maximum topics per world collection for display
WORLD_MAX_DISPLAY = 60


def filter_world_top(topics: list[dict], max_display: int = WORLD_MAX_DISPLAY) -> list[dict]:
    """For each world collection, keep only the top N topics by seeders.
    Non-world topics pass through unchanged."""
    world = []
    other = []
    for t in topics:
        if is_world_collection(t.get('collection')):
            world.append(t)
        else:
            other.append(t)

    by_collection: dict[str, list[dict]] = {}
    for t in world:
        coll = t.get('collection', '')
        by_collection.setdefault(coll, []).append(t)

    for coll in by_collection:
        by_collection[coll].sort(key=lambda x: -(x.get('seeders') or 0))
        removed = len(by_collection[coll]) - max_display
        if removed > 0:
            print(f"  {coll}: обрезано {removed} тем (оставлено топ-{max_display} по сидам)")
        by_collection[coll] = by_collection[coll][:max_display]

    result = other[:]
    for coll in by_collection:
        result.extend(by_collection[coll])
    return result
