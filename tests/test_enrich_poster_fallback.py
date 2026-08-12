import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import enrich_service
import generate_page


class EnrichPosterFallbackTests(unittest.TestCase):
    def _backend(self, search_result):
        backend = Mock()
        backend.has_real_poster.side_effect = (
            lambda topic: bool(topic.get("poster_url"))
        )
        backend.search_imdb.return_value = search_result
        backend.download_poster.return_value = "data/posters/tt36304003.jpg"
        return backend

    def test_recovers_poster_for_matching_known_imdb_id(self):
        topic = {"imdb_id": "tt36304003", "poster_url": ""}
        backend = self._backend(
            {
                "id": "tt36304003",
                "poster": "https://m.media-amazon.com/poster.jpg",
            }
        )

        with patch.object(enrich_service, "_backend", return_value=backend):
            recovered = enrich_service._search_known_imdb_poster(
                topic,
                "72 Hours",
                "2026",
            )

        self.assertTrue(recovered)
        self.assertEqual(
            topic["poster_url"],
            "data/posters/tt36304003.jpg",
        )
        backend.download_poster.assert_called_once_with(
            "tt36304003",
            "https://m.media-amazon.com/poster.jpg",
        )

    def test_rejects_poster_from_different_imdb_title(self):
        topic = {"imdb_id": "tt36304003", "poster_url": ""}
        backend = self._backend(
            {
                "id": "tt99999999",
                "poster": "https://m.media-amazon.com/wrong.jpg",
            }
        )

        with patch.object(enrich_service, "_backend", return_value=backend):
            recovered = enrich_service._search_known_imdb_poster(
                topic,
                "72 Hours",
                "2026",
            )

        self.assertFalse(recovered)
        self.assertEqual(topic["poster_url"], "")
        backend.download_poster.assert_not_called()

    def test_rejects_cached_kinopoisk_placeholder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            poster = Path(temp_dir) / "kp_13117127.png"
            poster.write_bytes(b"known-placeholder")
            topic = {
                "kp_id": "13117127",
                "poster_url": "data/posters/kp_13117127.png",
            }

            with (
                patch.object(generate_page, "POSTERS_DIR", temp_dir),
                patch.object(
                    generate_page,
                    "is_invalid_poster_file",
                    return_value=True,
                ),
            ):
                self.assertFalse(generate_page.has_real_poster(topic))

    def test_reuses_valid_poster_from_cached_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            posters = root / "posters"
            posters.mkdir()
            (posters / "6882447.jpg").write_bytes(b"real-poster")
            cache = root / "torrents.json"
            cache.write_text(
                json.dumps(
                    [
                        {
                            "topic_id": "6882447",
                            "imdb_id": "tt33372918",
                            "kp_id": "13117127",
                            "poster_url": "data/posters/6882447.jpg",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            topic = {
                "topic_id": "tpb_hash",
                "imdb_id": "tt33372918",
                "kp_id": "13117127",
                "poster_url": "",
            }

            with (
                patch.object(generate_page, "POSTERS_DIR", str(posters)),
                patch.object(generate_page, "TORRENTS_CACHE", str(cache)),
            ):
                resolved = generate_page.resolve_catalog_duplicate_poster(topic)

            self.assertTrue(resolved)
            self.assertEqual(
                topic["poster_url"],
                "data/posters/6882447.jpg",
            )


if __name__ == "__main__":
    unittest.main()
