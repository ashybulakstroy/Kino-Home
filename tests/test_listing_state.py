import unittest

from generate_page import apply_collection_listing_state


class ListingStateTests(unittest.TestCase):
    def test_current_page_keeps_server_order_and_history_moves_after_it(self):
        topics = [
            {
                "topic_id": "old-newer",
                "collection": "nashe_kino",
                "listing_order": 0,
                "date_str": "2026-07-20 12:00",
            },
            {
                "topic_id": "current-2",
                "collection": "nashe_kino",
                "listing_order": 1,
                "date_str": "2026-07-01 10:00",
            },
            {
                "topic_id": "old-older",
                "collection": "nashe_kino",
                "listing_order": 0,
                "date_str": "2026-07-10 12:00",
            },
            {
                "topic_id": "current-1",
                "collection": "nashe_kino",
                "listing_order": 5,
                "date_str": "2026-07-01 10:00",
            },
        ]
        listing_state = {
            "current-1": {
                "listing_order": 0,
                "date_str": "2026-07-27 18:02",
            },
            "current-2": {
                "listing_order": 1,
                "date_str": "2026-07-27 16:20",
            },
        }

        changed = apply_collection_listing_state(
            topics,
            "nashe_kino",
            listing_state,
        )

        self.assertTrue(changed)
        by_id = {topic["topic_id"]: topic for topic in topics}
        self.assertEqual(by_id["current-1"]["listing_order"], 0)
        self.assertEqual(by_id["current-2"]["listing_order"], 1)
        self.assertEqual(by_id["old-newer"]["listing_order"], 2)
        self.assertEqual(by_id["old-older"]["listing_order"], 3)
        self.assertEqual(
            by_id["current-1"]["date_str"],
            "2026-07-27 18:02",
        )
        self.assertEqual(
            len({topic["listing_order"] for topic in topics}),
            len(topics),
        )


if __name__ == "__main__":
    unittest.main()
