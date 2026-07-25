import unittest

from trailer_fallback import score_candidate, topic_title_variants


class TrailerFallbackTests(unittest.TestCase):
    def test_collects_all_parsed_title_variants(self):
        topic = {
            "movie_title": "Невидимый бой",
            "orig_title": "Nähtamatuvõitlus",
            "title": "Невидимый бой / Nähtamatu võitlus / The Invisible Fight (2023)",
        }

        self.assertEqual(
            topic_title_variants(topic),
            [
                "Невидимый бой",
                "Nähtamatuvõitlus",
                "Nähtamatu võitlus",
                "The Invisible Fight",
            ],
        )

    def test_accepts_matching_trailer_without_year(self):
        score = score_candidate(
            {
                "title": "The Invisible Fight | Official Trailer",
                "channel": "North Sky Film",
            },
            ["Невидимый бой", "The Invisible Fight"],
            2023,
        )

        self.assertGreaterEqual(score, 85)

    def test_accepts_matching_teaser(self):
        score = score_candidate(
            {"title": "18 килогерц - Тизер 1080p", "channel": "Что в кино"},
            ["18 килогерц", "18 kHz"],
            2020,
        )

        self.assertGreaterEqual(score, 85)

    def test_rejects_wrong_movie_with_same_year(self):
        score = score_candidate(
            {
                "title": "Arrival Trailer (2016) - Paramount Pictures",
                "channel": "Paramount Pictures",
            },
            ["Районы", "Rayony"],
            2016,
        )

        self.assertEqual(score, 0)

    def test_rejects_review(self):
        score = score_candidate(
            {"title": "Районы (2016) обзор фильма", "channel": "Movie Blog"},
            ["Районы", "Rayony"],
            2016,
        )

        self.assertEqual(score, 0)

    def test_rejects_ambiguous_one_word_title_without_year(self):
        score = score_candidate(
            {
                "title": "СТАРТРЕК: БЕСКОНЕЧНОСТЬ | Трейлер #3",
                "channel": "Paramount Pictures",
            },
            ["Бесконечность"],
            1992,
        )

        self.assertEqual(score, 0)

    def test_rejects_game_with_matching_title_and_year(self):
        score = score_candidate(
            {
                "title": "MARVEL ГЕРОИ ОМЕГА / PS4 - ТРЕЙЛЕР 2017",
                "channel": "PlayStation",
            },
            ["Герои"],
            2017,
        )

        self.assertEqual(score, 0)

    def test_accepts_one_word_title_with_exact_year(self):
        score = score_candidate(
            {
                "title": "Владивосток — Трейлер - Фильм 2021",
                "channel": "Киноафиша",
            },
            ["Владивосток"],
            2021,
        )

        self.assertGreaterEqual(score, 85)

    def test_accepts_exact_one_word_title_from_trusted_channel(self):
        score = score_candidate(
            {"title": "Зере - Трейлер", "channel": "что в кино"},
            ["Зере", "Zere"],
            2021,
        )

        self.assertGreaterEqual(score, 85)


if __name__ == "__main__":
    unittest.main()
