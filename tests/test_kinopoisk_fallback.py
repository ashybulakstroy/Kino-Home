import unittest
from unittest.mock import patch

from enrich_service import (
    find_kinopoisk_id_fallback,
    kinopoisk_title_variants,
    score_kinopoisk_title_candidate,
    select_kinopoisk_title_candidate,
)


class KinopoiskFallbackTests(unittest.TestCase):
    def test_collects_parsed_title_variants(self):
        topic = {
            "movie_title": "Невидимый бой",
            "orig_title": "Nähtamatuvõitlus",
            "title": "Невидимый бой / Nähtamatu võitlus / The Invisible Fight (2023)",
        }

        self.assertEqual(
            kinopoisk_title_variants(topic),
            [
                "Невидимый бой",
                "Nähtamatuvõitlus",
                "Nähtamatu võitlus",
                "The Invisible Fight",
            ],
        )

    def test_accepts_exact_title_and_year(self):
        candidate = {
            "label": "Временные трудности",
            "years": [2018],
            "kp_id": "972745",
        }

        self.assertGreaterEqual(
            score_kinopoisk_title_candidate(
                "Временные трудности",
                2018,
                candidate,
            ),
            90,
        )

    def test_accepts_adjacent_release_year(self):
        candidate = {
            "label": "Таинственная стена",
            "years": [1967],
            "kp_id": "42166",
        }

        self.assertGreaterEqual(
            score_kinopoisk_title_candidate(
                "Таинственная стена",
                1968,
                candidate,
            ),
            90,
        )

    def test_rejects_wrong_year(self):
        candidate = {
            "label": "Семейное счастье",
            "years": [2015],
            "kp_id": "1",
        }

        self.assertEqual(
            score_kinopoisk_title_candidate(
                "Семейное счастье",
                1970,
                candidate,
            ),
            0,
        )

    def test_rejects_partial_one_word_match(self):
        candidate = {
            "label": "Мортал Комбат",
            "years": [2021],
            "kp_id": "1",
        }

        self.assertEqual(
            score_kinopoisk_title_candidate("Комбат", 2021, candidate),
            0,
        )

    def test_rejects_ambiguous_candidates(self):
        candidates = [
            {"label": "Кукла", "years": [2026], "kp_id": "100"},
            {"label": "Кукла", "years": [2026], "kp_id": "200"},
        ]

        self.assertIsNone(
            select_kinopoisk_title_candidate(["Кукла"], 2026, candidates)
        )

    @patch("enrich_service._kinopoisk_lookup_by_title")
    @patch(
        "enrich_service._kinopoisk_lookup_by_imdb",
        return_value=(True, None),
    )
    def test_does_not_use_title_fallback_when_imdb_is_known(
        self,
        _imdb_lookup,
        title_lookup,
    ):
        result = find_kinopoisk_id_fallback(
            object(),
            {
                "movie_title": "Джокер",
                "movie_year": "2013",
                "imdb_id": "tt3002286",
            },
            "Джокер",
            "2013",
            use_cache=False,
        )

        self.assertIsNone(result)
        title_lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
