# Session Context — LocaL-Kino

## Current Task
Redesign `/browse/shuffle` as a filmstrip: frames slide right-to-left through center poster, old poster slides left into strip, new frame from right becomes main poster.

## Changes Made (this session)

### stream_server.py
- `/browse/shuffle` redesigned as horizontal filmstrip
  - Full-width filmstrip at center height (top:50%)
  - Main poster (300×440) centered, overlapping strip (z-index 3 vs 2)
  - 25 frames (12 left + center + 12 right) at 100×150 each
  - Frames slide right-to-left with CSS transition (0.5s cubic-bezier)
  - Dual-layer poster animation: `#mpCur` slides left (`.out`), `#mpNext` slides in from right (`.in.go`)
  - Animation lock (`animating` flag) prevents overlap
  - Default speed: 5s
  - Buttons: 5с, 10с, 15с, 30с
  - Left/Right arrow keys for prev/next
- Placeholder poster path: `placeholder.jpg` → `placeholder.png`
- `pu()` function fallback updated
- Browse links bar on `/` (injected at runtime with versioned ETag)
- Format filter added to `/browse/filter`

### generate_page.py
- `sanitize_topic()` zeros ratings, IDs, magnet, YT URL; sets `_sanitized` flag
- `is_forbidden_topic()` checks genre/title against `FORBIDDEN_GENRES`
- Sanitization runs in 3 places:
  1. After `fetch_magnets()` for fresh topics
  2. After `enrich()` for topics with genre data
  3. After all processing for entire cache
- Enrichment skips already-sanitized topics (`_sanitized` or `imdb_id == '0'`)
- Cache loaded topics also sanitized on startup
- Placeholder path updated to `placeholder.png`

### Data fixes
- 45 poster URLs normalized from `posters/xxx.jpg` → `data/posters/xxx.jpg`
- 7 `placeholder.jpg` refs → `placeholder.png`
- Missing poster files: 3 movies with empty poster_url → falls back to placeholder

## Key Files
- `stream_server.py` — Flask server, all routes, shuffle logic
- `generate_page.py` — parsing, enrichment, sanitization
- `engine.py` — libtorrent engine
- `data/torrents_data.json` — movie cache (158 entries)
- `data/hidden_topics.json` — hidden topic IDs (12 total: 5 manual + 7 sanitized)

## Issues / Sticking Points
- Server crashes/not responding after recent edits to shuffle f-string
- Need to verify `next()` animation works correctly with dual poster layers
- The `if __name__ == '__main__':` was accidentally removed and re-added

## To Verify
1. `/browse/shuffle` loads correctly
2. Frames slide right-to-left when clicking → or auto-timer
3. Old poster slides left, new poster slides in from right
4. Strip and poster animations sync
5. All 6 browse modes work
6. Placeholder posters display for missing/broken images
