"""Find stream-friendly RuTracker alternatives for an AVI movie title.

Usage examples:
  python tools/find_streamable_rutracker.py "Притворщики" --year 2016
  python tools/find_streamable_rutracker.py --avi-title "Притворщики (Ольга Ланд) [2016, Мелодрама, SATRip]"
  python tools/find_streamable_rutracker.py "Острые козырьки: Бессмертный человек" --year 2026 --json

The tool uses public search-engine HTML pages, not a search API.
It is intentionally conservative: it ranks candidates, but does not modify data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import generate_page as gp  # noqa: E402


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

STREAMABLE_FORMATS = {"mkv", "mp4"}
GOOD_TERMS = ("mkv", "mp4", "avc", "h.264", "h264", "web-dl", "webdl", "hdrip", "bdrip")
BAD_TERMS = ("avi", "xvid", "divx", "satrip", "dvdrip")


@dataclass
class SearchHit:
    topic_id: str
    url: str
    search_title: str = ""
    source: str = ""
    topic_title: str = ""
    movie_title: str = ""
    orig_title: str = ""
    year: str = ""
    format: str = ""
    magnet: bool = False
    seeders: int = 0
    score: int = 0
    reasons: list[str] | None = None


def normalize_text(value: str) -> str:
    value = unescape(value or "").lower().replace("ё", "е")
    value = re.sub(r"\[[^\]]*\]|\([^\)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9а-я]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def title_tokens(value: str) -> set[str]:
    return {t for t in normalize_text(value).split() if len(t) >= 3}


def parse_input_title(raw: str, year: str = "") -> tuple[str, str, str]:
    movie_title, orig_title, parsed_year, _genre, _quality = gp.parse_rutracker_title(raw)
    return movie_title or raw, orig_title or "", year or parsed_year or ""


def topic_url(topic_id: str) -> str:
    return f"https://rutracker.net/forum/viewtopic.php?t={topic_id}"


def extract_topic_id(url: str) -> str:
    decoded = urllib.parse.unquote(url)
    match = re.search(r"rutracker\.(?:net|org)/forum/viewtopic\.php\?t=(\d+)", decoded, re.I)
    if match:
        return match.group(1)
    match = re.search(r"[?&]t=(\d+)", decoded)
    return match.group(1) if match and "rutracker" in decoded.lower() else ""


def unwrap_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ("uddg", "url", "u", "q"):
        if key in qs and qs[key]:
            return qs[key][0]
    return url


def make_queries(title: str, year: str = "") -> list[str]:
    quoted = f'"{title}"' if title else ""
    year_part = f" {year}" if year else ""
    return [
        f'site:rutracker.net/forum/viewtopic.php {quoted}{year_part} mkv',
        f'site:rutracker.net/forum/viewtopic.php {quoted}{year_part} mp4',
        f'site:rutracker.net/forum/viewtopic.php {quoted}{year_part} AVC',
        f'site:rutracker.org/forum/viewtopic.php {quoted}{year_part} mkv',
        f'site:rutracker.org/forum/viewtopic.php {quoted}{year_part} mp4',
    ]


def search_duckduckgo(session: requests.Session, query: str, limit: int) -> list[tuple[str, str]]:
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    response = session.get(url, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    hits: list[tuple[str, str]] = []
    for link in soup.select("a.result__a, a[href]"):
        href = unwrap_url(link.get("href") or "")
        topic_id = extract_topic_id(href)
        if not topic_id:
            continue
        title = link.get_text(" ", strip=True)
        hits.append((topic_url(topic_id), title))
        if len(hits) >= limit:
            break
    return hits


def search_bing(session: requests.Session, query: str, limit: int) -> list[tuple[str, str]]:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})
    response = session.get(url, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    hits: list[tuple[str, str]] = []
    for result in soup.select("li.b_algo h2 a, a[href]"):
        href = unwrap_url(result.get("href") or "")
        topic_id = extract_topic_id(href)
        if not topic_id:
            continue
        title = result.get_text(" ", strip=True)
        hits.append((topic_url(topic_id), title))
        if len(hits) >= limit:
            break
    return hits


def search_web(session: requests.Session, queries: list[str], per_query: int) -> list[SearchHit]:
    seen: set[str] = set()
    hits: list[SearchHit] = []
    engines = (("ddg", search_duckduckgo), ("bing", search_bing))
    for query in queries:
        print(f"Search: {query}", file=sys.stderr)
        for source, fn in engines:
            try:
                for url, title in fn(session, query, per_query):
                    topic_id = extract_topic_id(url)
                    if not topic_id or topic_id in seen:
                        continue
                    seen.add(topic_id)
                    hits.append(SearchHit(topic_id=topic_id, url=topic_url(topic_id), search_title=title, source=source))
            except Exception as exc:
                print(f"  {source}: {exc}", file=sys.stderr)
            time.sleep(0.2)
    return hits


def enrich_hit(hit: SearchHit, fetch_topic: bool) -> SearchHit:
    title = hit.search_title
    fmt = ""
    magnet = False
    if fetch_topic:
        try:
            html = gp.get_topic_html(hit.topic_id, hit.url, timeout=15)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                page_title = soup.find("title")
                title = page_title.get_text(" ", strip=True) if page_title else title
                title = re.sub(r"\s*::\s*.*$", "", title).strip()
                data = gp.parse_topic_for_magnet(html)
                fmt = data.get("format") or ""
                magnet = bool(data.get("magnet"))
        except Exception as exc:
            hit.reasons = [f"topic fetch failed: {exc}"]

    movie_title, orig_title, year, _genre, _quality = gp.parse_rutracker_title(title or hit.search_title)
    hit.topic_title = title or hit.search_title
    hit.movie_title = movie_title
    hit.orig_title = orig_title
    hit.year = year
    hit.format = fmt
    hit.magnet = magnet
    return hit


def score_hit(hit: SearchHit, wanted_title: str, wanted_year: str) -> SearchHit:
    reasons: list[str] = []
    score = 0
    wanted_tokens = title_tokens(wanted_title)
    candidate_text = " ".join([hit.topic_title, hit.search_title, hit.movie_title, hit.orig_title])
    candidate_tokens = title_tokens(candidate_text)
    overlap = len(wanted_tokens & candidate_tokens) / max(len(wanted_tokens), 1)

    if overlap >= 0.8:
        score += 50
        reasons.append("title overlap high")
    elif overlap >= 0.5:
        score += 25
        reasons.append("title overlap medium")
    else:
        score -= 40
        reasons.append("title overlap low")

    if wanted_year and hit.year == wanted_year:
        score += 25
        reasons.append("same year")
    elif wanted_year and hit.year and hit.year != wanted_year:
        score -= 50
        reasons.append(f"year mismatch {hit.year}")

    fmt = (hit.format or "").lower()
    text_norm = normalize_text(candidate_text)
    if fmt in STREAMABLE_FORMATS:
        score += 35
        reasons.append(f"format {fmt}")
    elif any(term in text_norm for term in ("mkv", "mp4")):
        score += 25
        reasons.append("streamable term in title")
    elif "avc" in text_norm:
        score += 15
        reasons.append("avc term in title")

    if any(term in text_norm for term in BAD_TERMS):
        score -= 20
        reasons.append("avi-like term")
    if hit.magnet:
        score += 15
        reasons.append("magnet found")

    hit.score = score
    hit.reasons = reasons
    return hit


def local_candidates(title: str, year: str, include_non_streamable: bool = False) -> list[SearchHit]:
    path = ROOT / "data" / "torrents_data.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text("utf-8"))
    hits: list[SearchHit] = []
    wanted_tokens = title_tokens(title)
    for topic in data:
        text = " ".join(str(topic.get(k) or "") for k in ("title", "movie_title", "orig_title"))
        if not wanted_tokens or len(wanted_tokens & title_tokens(text)) / len(wanted_tokens) < 0.5:
            continue
        if year and str(topic.get("movie_year") or "") != year:
            continue
        topic_id = str(topic.get("topic_id") or "")
        if not topic_id:
            continue
        fmt = str(topic.get("format") or "")
        if fmt and fmt.lower() not in STREAMABLE_FORMATS and not include_non_streamable:
            continue
        hit = SearchHit(
            topic_id=topic_id,
            url=topic.get("topic_url") or topic_url(topic_id),
            search_title=topic.get("title") or "",
            source="local",
            topic_title=topic.get("title") or "",
            movie_title=topic.get("movie_title") or "",
            orig_title=topic.get("orig_title") or "",
            year=str(topic.get("movie_year") or ""),
            format=fmt,
            magnet=bool(topic.get("magnet")),
            seeders=int(topic.get("seeders") or 0),
        )
        hits.append(hit)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="Find MKV/MP4 RuTracker alternatives for an AVI movie.")
    parser.add_argument("title", nargs="?", help="Movie title or raw AVI topic title")
    parser.add_argument("--avi-title", help="Raw AVI topic title; parsed to title/year")
    parser.add_argument("--year", default="", help="Movie year")
    parser.add_argument("--limit", type=int, default=10, help="Results per search query")
    parser.add_argument("--no-fetch", action="store_true", help="Do not fetch RuTracker topics for magnet/format")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument("--no-local", action="store_true", help="Skip local catalog candidates")
    parser.add_argument("--include-non-streamable", action="store_true", help="Show known AVI/non-streamable hits too")
    args = parser.parse_args()

    raw = args.avi_title or args.title
    if not raw:
        parser.error("title or --avi-title is required")

    wanted_title, orig_title, wanted_year = parse_input_title(raw, args.year)
    search_title = orig_title or wanted_title
    session = requests.Session()
    session.headers.update(HEADERS)

    hits: list[SearchHit] = []
    if not args.no_local:
        hits.extend(local_candidates(wanted_title, wanted_year, args.include_non_streamable))

    web_hits = search_web(session, make_queries(search_title, wanted_year), args.limit)
    seen = {hit.topic_id for hit in hits}
    for hit in web_hits:
        if hit.topic_id not in seen:
            hits.append(hit)
            seen.add(hit.topic_id)

    scored: list[SearchHit] = []
    for hit in hits:
        if hit.source != "local":
            enrich_hit(hit, not args.no_fetch)
        if hit.format and hit.format.lower() not in STREAMABLE_FORMATS and not args.include_non_streamable:
            continue
        scored.append(score_hit(hit, wanted_title, wanted_year))

    scored.sort(key=lambda h: (h.score, h.seeders), reverse=True)

    if args.json:
        print(json.dumps([asdict(hit) for hit in scored], ensure_ascii=False, indent=2))
    else:
        print(f"Input: {raw}")
        print(f"Parsed: {wanted_title} ({wanted_year or 'year?'})")
        print()
        for hit in scored[:20]:
            fmt = hit.format or "?"
            magnet = "magnet" if hit.magnet else "no magnet"
            print(f"[{hit.score:>3}] t={hit.topic_id} {fmt} {magnet} src={hit.source} seeds={hit.seeders}")
            print(f"      {hit.topic_title or hit.search_title}")
            print(f"      {hit.url}")
            print(f"      reasons: {', '.join(hit.reasons or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
