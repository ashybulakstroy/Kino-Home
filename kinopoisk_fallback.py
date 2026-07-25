"""Verified Wikidata fallback for missing Kinopoisk film IDs."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from config import DATA_DIR, ENRICH_NO_CHANGE_RETRY_DAYS
from project_io import atomic_write_json


SPARQL_URL = "https://query.wikidata.org/sparql"
CACHE_PATH = DATA_DIR / "kp_fallback_cache.json"
USER_AGENT = "KinoGallery/1.0 (local media catalog)"

_REQUEST_SLOTS = threading.BoundedSemaphore(3)
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _normalize(value):
    value = str(value or "").lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def _title_tokens(value):
    ignored = {"film", "movie", "фильм", "кино"}
    return {
        token for token in _normalize(value).split()
        if token not in ignored and len(token) >= 2
    }


def topic_title_variants(topic, fallback_title=""):
    values = [
        topic.get("movie_title"),
        topic.get("orig_title"),
        fallback_title,
    ]
    raw = str(topic.get("title") or "")
    title_part = raw.split("(", 1)[0].split("[", 1)[0]
    values.extend(part.strip() for part in title_part.split("/"))

    result = []
    seen = set()
    for value in values:
        value = re.sub(r"\s+", " ", str(value or "")).strip(" ._-")
        key = _normalize(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result[:5]


def score_title_candidate(wanted_title, wanted_year, candidate):
    label = candidate.get("label") or ""
    wanted = _normalize(wanted_title)
    actual = _normalize(label)
    wanted_tokens = _title_tokens(wanted)
    actual_tokens = _title_tokens(actual)
    if not wanted or not actual or not wanted_tokens or not actual_tokens:
        return 0

    try:
        year = int(wanted_year or 0)
    except (TypeError, ValueError):
        year = 0
    years = {
        int(value) for value in candidate.get("years", [])
        if str(value).isdigit()
    }
    if year:
        if not years or min(abs(value - year) for value in years) > 1:
            return 0

    overlap = len(wanted_tokens & actual_tokens) / max(len(wanted_tokens), 1)
    exact_tokens = wanted_tokens == actual_tokens
    if len(wanted_tokens) == 1 and not exact_tokens:
        return 0
    if not exact_tokens and overlap < 0.8:
        return 0

    score = 100 if wanted == actual else 75 if exact_tokens else int(overlap * 70)
    score += int(SequenceMatcher(None, wanted, actual).ratio() * 20)
    if year and years:
        distance = min(abs(value - year) for value in years)
        score += 20 if distance == 0 else 10
    return score


def select_title_candidate(title_variants, year, candidates):
    ranked = []
    for candidate in candidates:
        kp_id = str(candidate.get("kp_id") or "")
        if not kp_id.isdigit():
            continue
        score = max(
            (score_title_candidate(title, year, candidate) for title in title_variants),
            default=0,
        )
        if score:
            ranked.append((score, kp_id, candidate))
    if not ranked:
        return None

    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, best_id, best = ranked[0]
    competing_ids = {
        kp_id for score, kp_id, _candidate in ranked
        if score >= best_score - 5
    }
    if len(competing_ids) > 1:
        return None
    return {**best, "kp_id": best_id, "score": best_score}


def _sparql_rows(session, query):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
    for attempt in range(3):
        try:
            with _REQUEST_SLOTS:
                response = session.get(
                    SPARQL_URL,
                    params={"query": query, "format": "json"},
                    headers=headers,
                    timeout=60,
                )
            if response.status_code == 200:
                data = response.json()
                return True, data.get("results", {}).get("bindings", [])
            if response.status_code not in (429, 500, 502, 503, 504):
                return True, []
        except Exception:
            pass
        time.sleep(attempt + 1)
    return False, []


def _sparql_literal(value):
    return json.dumps(str(value or ""), ensure_ascii=False)


def _lookup_by_imdb(session, imdb_id):
    query = (
        "SELECT ?item ?kp WHERE { "
        f"?item wdt:P345 {_sparql_literal(imdb_id)}; wdt:P2603 ?kp. "
        "} LIMIT 5"
    )
    ok, rows = _sparql_rows(session, query)
    if not ok:
        return False, None
    candidates = []
    for row in rows:
        kp_id = str(row.get("kp", {}).get("value") or "")
        qid = str(row.get("item", {}).get("value") or "").rsplit("/", 1)[-1]
        if kp_id.isdigit():
            candidates.append((kp_id, qid))
    unique_ids = {kp_id for kp_id, _qid in candidates}
    if len(unique_ids) != 1:
        return True, None
    kp_id, qid = candidates[0]
    return True, {
        "kp_id": kp_id,
        "wikidata_id": qid,
        "source": "wikidata-imdb",
    }


def _entity_search_query(title):
    return f"""
SELECT ?item ?itemLabel ?kp ?date WHERE {{
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:endpoint "www.wikidata.org";
                    wikibase:api "EntitySearch";
                    mwapi:search {_sparql_literal(title)};
                    mwapi:language "ru".
    ?item wikibase:apiOutputItem mwapi:item.
  }}
  ?item wdt:P2603 ?kp.
  OPTIONAL {{ ?item wdt:P577 ?date. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ru,en". }}
}}
LIMIT 12
""".strip()


def _rows_to_candidates(rows):
    grouped = {}
    for row in rows:
        kp_id = str(row.get("kp", {}).get("value") or "")
        qid = str(row.get("item", {}).get("value") or "").rsplit("/", 1)[-1]
        if not kp_id.isdigit() or not qid:
            continue
        key = (qid, kp_id)
        candidate = grouped.setdefault(key, {
            "kp_id": kp_id,
            "wikidata_id": qid,
            "label": str(row.get("itemLabel", {}).get("value") or ""),
            "years": set(),
            "source": "wikidata-title",
        })
        date_value = str(row.get("date", {}).get("value") or "")
        match = re.match(r"([+-]?\d{4})", date_value)
        if match:
            candidate["years"].add(abs(int(match.group(1))))
    result = []
    for candidate in grouped.values():
        candidate["years"] = sorted(candidate["years"])
        result.append(candidate)
    return result


def _lookup_by_title(session, variants, year):
    all_candidates = []
    request_succeeded = False
    for title in variants[:3]:
        ok, rows = _sparql_rows(session, _entity_search_query(title))
        request_succeeded = request_succeeded or ok
        if ok:
            all_candidates.extend(_rows_to_candidates(rows))
        candidate = select_title_candidate(variants, year, all_candidates)
        if candidate and candidate.get("score", 0) >= 90:
            return True, candidate
    return request_succeeded, select_title_candidate(variants, year, all_candidates)


def _cache_key(topic, title, year):
    imdb_id = str(topic.get("imdb_id") or "").strip()
    if imdb_id and imdb_id != "0":
        return f"imdb:{imdb_id}"
    return f"title:{_normalize(title)}|{year or ''}"


def _key_lock(key):
    with _KEY_LOCKS_GUARD:
        return _KEY_LOCKS.setdefault(key, threading.Lock())


def _read_cache_entry(key):
    with _CACHE_LOCK:
        try:
            cache = json.loads(CACHE_PATH.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, None
        entry = cache.get(key)
    if not isinstance(entry, dict):
        return False, None
    try:
        checked = datetime.fromisoformat(str(entry.get("checked_at"))).date()
    except (TypeError, ValueError):
        return False, None
    ttl = max(1, ENRICH_NO_CHANGE_RETRY_DAYS)
    if date.today() - checked >= timedelta(days=ttl):
        return False, None
    return True, entry.get("result")


def _write_cache_entry(key, result):
    with _CACHE_LOCK:
        try:
            cache = json.loads(CACHE_PATH.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
        cache[key] = {
            "checked_at": date.today().isoformat(),
            "result": result,
        }
        atomic_write_json(CACHE_PATH, cache)


def find_kinopoisk_id_fallback(session, topic, title, year, use_cache=True):
    variants = topic_title_variants(topic, title)
    if not variants:
        return None
    key = _cache_key(topic, variants[0], year)

    with _key_lock(key):
        if use_cache:
            cached, result = _read_cache_entry(key)
            if cached:
                return result

        imdb_id = str(topic.get("imdb_id") or "").strip()
        request_ok = False
        result = None
        if imdb_id and imdb_id != "0":
            request_ok, result = _lookup_by_imdb(session, imdb_id)
        else:
            title_ok, result = _lookup_by_title(session, variants, year)
            request_ok = request_ok or title_ok

        if use_cache and request_ok:
            _write_cache_entry(key, result)
        return result
