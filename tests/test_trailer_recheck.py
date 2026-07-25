import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_page


class TrailerRecheckTests(unittest.TestCase):
    def test_scores_candidate_against_original_title(self):
        topic = {
            "movie_title": "Джек Райан: Призрачная война",
            "orig_title": "Tom Clancy's Jack Ryan: Ghost War",
            "movie_year": "2026",
        }
        candidate = {
            "title": "JACK RYAN: GHOST WAR Official Trailer (2026)",
            "channel": "Prime Video",
            "length": "2:00",
        }

        self.assertGreaterEqual(
            generate_page._score_topic_youtube_candidate(topic, candidate),
            50,
        )

    def test_rejects_same_year_trailer_with_unrelated_title(self):
        topic = {
            "movie_title": "Джек Райан: Призрачная война",
            "orig_title": "Tom Clancy's Jack Ryan: Ghost War",
            "movie_year": "2026",
        }
        candidate = {
            "title": "Скуф — Трейлер (2026)",
            "channel": "HypeFilms",
            "length": "2:00",
        }

        self.assertEqual(
            generate_page._score_topic_youtube_candidate(topic, candidate),
            0,
        )

    def test_invalidates_only_matching_topic_cache_entries(self):
        rejected = "https://www.youtube.com/watch?v=LjZXcStSMx8"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kp_cache = root / "kp.json"
            imdb_cache = root / "imdb.json"
            youtube_cache = root / "youtube.json"
            kp_cache.write_text(
                json.dumps({"6458125": rejected, "other": rejected}),
                encoding="utf-8",
            )
            imdb_cache.write_text(
                json.dumps({"tt34378301": rejected}),
                encoding="utf-8",
            )
            youtube_cache.write_text(
                json.dumps({"tom clancy's jack ryan: ghost war|2026": rejected}),
                encoding="utf-8",
            )
            topic = {
                "kp_id": "6458125",
                "imdb_id": "tt34378301",
                "orig_title": "Tom Clancy's Jack Ryan: Ghost War",
                "movie_year": "2026",
            }

            with (
                patch.object(generate_page, "KP_TRAILER_CACHE", str(kp_cache)),
                patch.object(generate_page, "IMDB_TRAILER_CACHE", str(imdb_cache)),
                patch.object(generate_page, "YOUTUBE_CACHE", str(youtube_cache)),
            ):
                removed = generate_page.invalidate_trailer_cache_for_topic(
                    topic,
                    rejected,
                )

            self.assertEqual(removed, 3)
            self.assertEqual(
                json.loads(kp_cache.read_text("utf-8")),
                {"other": rejected},
            )
            self.assertEqual(json.loads(imdb_cache.read_text("utf-8")), {})
            self.assertEqual(json.loads(youtube_cache.read_text("utf-8")), {})


if __name__ == "__main__":
    unittest.main()
