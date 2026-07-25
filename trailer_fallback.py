"""Conservative multi-title YouTube fallback for missing trailers."""

import json
import re
import urllib.parse
from difflib import SequenceMatcher
from html import unescape


POSITIVE_WORDS = ("trailer", "трейлер", "teaser", "тизер")
NEGATIVE_WORDS = (
    "review", "reaction", "explained", "ending", "soundtrack", "song",
    "clip", "scene", "interview", "behind the scenes", "gameplay",
    "season", "series", "episode", "ps4", "ps5", "xbox", "nintendo",
    "обзор", "реакция", "разбор", "концовка", "саундтрек", "песня",
    "клип", "сцена", "интервью", "со съемок", "серия", "эпизод",
    "сезон", "сериал", "игра",
)
TRUSTED_CHANNEL_WORDS = (
    "kinopoisk", "кинопоиск", "netflix", "disney", "warner",
    "paramount", "universal", "sony", "film festival", "киноафиша",
    "трейлеры", "что в кино", "arrow video",
)

_CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})


def _json_text(value):
    if not value:
        return ""
    try:
        return unescape(json.loads(f'"{value}"'))
    except Exception:
        return unescape(value)


def _compact(value):
    value = unescape(str(value or "")).lower().replace("ё", "е")
    value = value.replace("khz", "kilohertz").replace("кгц", "килогерц")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def _latin(value):
    return _compact(value).translate(_CYRILLIC_TO_LATIN)


def _identity_tokens(value):
    ignored = {
        "official", "trailer", "teaser", "movie", "film", "hd", "russkiy",
        "ofitsialnyy", "treyler", "tizer", "kino", "subtitry",
    }
    return {
        token for token in _latin(value).split()
        if token not in ignored and not re.fullmatch(r"(?:19|20)\d{2}", token)
        and len(token) >= 2
    }


def topic_title_variants(topic):
    """Return distinct parsed names without changing persisted topic titles."""
    values = [
        topic.get("movie_title"),
        topic.get("orig_title"),
    ]
    raw = str(topic.get("title") or "")
    title_part = raw.split("(", 1)[0].split("[", 1)[0]
    values.extend(part.strip() for part in title_part.split("/"))

    result = []
    seen = set()
    for value in values:
        value = re.sub(r"\s+", " ", str(value or "")).strip(" ._-")
        key = _compact(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result[:5]


def _youtube_candidates(html):
    candidates = []
    seen = set()
    for match in re.finditer(r'"videoId":"([a-zA-Z0-9_-]{11})"', html or ""):
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        chunk = html[match.start():match.start() + 3500]
        title_match = re.search(r'"title":\{"runs":\[\{"text":"(.*?)"', chunk)
        if not title_match:
            title_match = re.search(r'"title":\{"simpleText":"(.*?)"', chunk)
        if not title_match:
            continue
        channel_match = re.search(r'"ownerText":\{"runs":\[\{"text":"(.*?)"', chunk)
        candidates.append({
            "video_id": video_id,
            "title": _json_text(title_match.group(1)),
            "channel": _json_text(channel_match.group(1)) if channel_match else "",
        })
        if len(candidates) >= 20:
            break
    return candidates


def score_candidate(candidate, title_variants, year):
    """Score identity and trailer intent; return zero for unsafe matches."""
    candidate_title = _compact(candidate.get("title"))
    candidate_latin = _latin(candidate_title)
    channel = _compact(candidate.get("channel"))
    if not candidate_title or any(word in candidate_title for word in NEGATIVE_WORDS):
        return 0
    if not any(word in candidate_title for word in POSITIVE_WORDS):
        return 0

    candidate_tokens = _identity_tokens(candidate_title)
    best_identity = 0
    best_variant_tokens = set()
    for variant in title_variants:
        variant_latin = _latin(variant)
        variant_tokens = _identity_tokens(variant)
        if not variant_latin or not variant_tokens:
            continue
        score = 0
        if variant_latin in candidate_latin:
            score += 60
        overlap = len(variant_tokens & candidate_tokens) / max(len(variant_tokens), 1)
        score += int(overlap * 45)
        similarity = SequenceMatcher(None, variant_latin, candidate_latin).ratio()
        score += int(similarity * 20)
        if score > best_identity:
            best_identity = score
            best_variant_tokens = variant_tokens

    if best_identity < 45:
        return 0

    candidate_years = {
        int(value) for value in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", candidate_title)
    }
    try:
        wanted_year = int(year or 0)
    except (TypeError, ValueError):
        wanted_year = 0

    year_distance = None
    if wanted_year and candidate_years:
        year_distance = min(abs(value - wanted_year) for value in candidate_years)
        if year_distance > 1:
            return 0

    # A one-word movie is too ambiguous without an exact year. Even with the
    # year present, reject candidates carrying other unexplained title words.
    trusted_channel = any(word in channel for word in TRUSTED_CHANNEL_WORDS)
    if len(best_variant_tokens) == 1:
        extra_tokens = candidate_tokens - best_variant_tokens
        exact_year_match = year_distance == 0 and len(extra_tokens) <= 1
        trusted_exact_match = (
            not candidate_years and not extra_tokens and trusted_channel
        )
        if not exact_year_match and not trusted_exact_match:
            return 0

    score = best_identity
    if "trailer" in candidate_title or "трейлер" in candidate_title:
        score += 30
    else:
        score += 22
    if "official" in candidate_title or "официальн" in candidate_title:
        score += 8
    if trusted_channel:
        score += 8

    if wanted_year and candidate_years:
        if year_distance == 0:
            score += 15
        elif year_distance == 1:
            score -= 5
    return score


def _oembed(session, video_url):
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        video_url, safe=""
    )
    response = session.get(url, timeout=8)
    if response.status_code != 200:
        return None
    return response.json()


def search_topic_trailer_fallback(session, topic):
    """Search all known title variants and return a verified YouTube URL."""
    variants = topic_title_variants(topic)
    if not variants:
        return None
    year = topic.get("movie_year") or topic.get("year") or ""

    queries = []
    for variant in variants[:3]:
        if re.search(r"[а-яё]", variant, flags=re.I):
            queries.append(f'"{variant}" {year} трейлер тизер')
        queries.append(f'"{variant}" {year} trailer teaser')

    ranked = {}
    for query in queries[:6]:
        try:
            url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
            response = session.get(url, timeout=10)
            if response.status_code != 200:
                continue
        except Exception:
            continue
        for candidate in _youtube_candidates(response.text):
            score = score_candidate(candidate, variants, year)
            video_id = candidate["video_id"]
            if score > ranked.get(video_id, {}).get("score", 0):
                ranked[video_id] = {**candidate, "score": score}

    for candidate in sorted(ranked.values(), key=lambda item: item["score"], reverse=True)[:3]:
        if candidate["score"] < 95:
            continue
        video_url = f'https://www.youtube.com/watch?v={candidate["video_id"]}'
        try:
            metadata = _oembed(session, video_url)
        except Exception:
            continue
        if not metadata:
            continue
        verified = {
            "title": metadata.get("title", ""),
            "channel": metadata.get("author_name", ""),
        }
        if score_candidate(verified, variants, year) >= 85:
            return video_url
    return None
