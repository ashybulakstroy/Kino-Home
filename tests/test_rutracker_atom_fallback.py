import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import generate_page


ATOM = '''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>[ÐÐ±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¾] ÐÐ¾ÑÐ¸ÑÐµÐ½Ð¸Ðµ Ð²ÐµÐºÐ° [1981, ÐÐ¾Ð¼ÐµÐ´Ð¸Ñ, DVDRip] [1.05 GB]</title>
    <id>tag:rto.feed,2026-08-09:/t/6875454</id>
    <updated>2026-08-09T18:29:14+00:00</updated>
    <link href="https://rutracker.org/forum/viewtopic.php?t=6875454" />
  </entry>
</feed>'''.encode('utf-8')


class RutrackerAtomFallbackTests(unittest.TestCase):
    def test_parses_atom_as_listing_topic(self):
        topics = generate_page.parse_rutracker_atom_feed(ATOM, 'nashe_kino')

        self.assertEqual(len(topics), 1)
        topic = topics[0]
        self.assertEqual(topic['topic_id'], '6875454')
        self.assertEqual(topic['movie_title'], 'ÐÐ¾ÑÐ¸ÑÐµÐ½Ð¸Ðµ Ð²ÐµÐºÐ°')
        self.assertEqual(topic['movie_year'], '1981')
        self.assertEqual(topic['size_str'], '1.05 GB')
        self.assertEqual(topic['size_bytes'], int(1.05 * 1024**3))
        self.assertEqual(topic['_listing_source'], 'rutracker_atom')
        self.assertNotIn('seeders', topic)

    def test_detects_cloudflare_challenge(self):
        class Response:
            status_code = 403
            headers = {'server': 'cloudflare'}
            text = '<title>Just a moment...</title><div id="cf-chl-widget"></div>'

        self.assertTrue(generate_page.is_cloudflare_challenge_response(Response()))

    def test_topic_detail_stops_immediately_on_cloudflare(self):
        response = Mock()
        response.status_code = 403
        response.headers = {'server': 'cloudflare'}
        response.text = '<title>Just a moment...</title>'
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(generate_page, 'TOPIC_CACHE_DIR', temp_dir),
                patch.object(generate_page.SESSION, 'get', return_value=response) as get,
            ):
                result = generate_page.get_topic_html(
                    '6891549',
                    'https://rutracker.org/forum/viewtopic.php?t=6891549',
                )

        self.assertIsNone(result)
        get.assert_called_once()

    def test_atom_topic_is_persisted_outside_playable_catalog(self):
        topic = generate_page.parse_rutracker_atom_feed(ATOM, 'nashe_kino')[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            pending_path = Path(temp_dir) / 'rutracker_pending_topics.json'
            with patch.object(generate_page, 'RUTRACKER_PENDING_TOPICS', str(pending_path)):
                pending, added = generate_page.merge_rutracker_pending_topics(
                    [],
                    [topic],
                    reason='atom_without_magnet',
                )
                self.assertEqual(added, 1)
                self.assertTrue(generate_page.save_rutracker_pending_topics(pending))
                self.assertEqual(generate_page.clean_catalog_topics([topic]), [])

                restored = generate_page.load_rutracker_pending_topics()

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]['topic_id'], '6875454')
        self.assertEqual(restored[0]['_pending_reason'], 'atom_without_magnet')
        self.assertTrue(restored[0]['_pending_first_seen_at'])

    def test_pending_topic_is_retried_and_removed_after_promotion(self):
        topic = generate_page.parse_rutracker_atom_feed(ATOM, 'nashe_kino')[0]
        pending, _ = generate_page.merge_rutracker_pending_topics(
            [],
            [topic],
            reason='atom_without_magnet',
        )
        pending[0]['_magnet_failed'] = True

        candidates = generate_page.rutracker_pending_candidates(
            pending,
            'nashe_kino',
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['_listing_source'], 'rutracker_pending')
        self.assertNotIn('_magnet_failed', candidates[0])

        magnet = 'magnet:?xt=urn:btih:' + ('a' * 40)
        with (
            patch.object(generate_page, 'WORKER_COUNT', 1),
            patch.object(generate_page, 'get_topic_html', return_value='<html></html>'),
            patch.object(
                generate_page,
                'parse_topic_for_magnet',
                return_value={'magnet': magnet, 'imdb': '', 'poster': '', 'format': 'mkv'},
            ),
        ):
            stats = generate_page.fetch_magnets(candidates)

        self.assertEqual(stats, {'total': 1, 'ok': 1, 'failed': 0})
        self.assertEqual(candidates[0]['magnet'], magnet)
        self.assertEqual(candidates[0]['format'], 'mkv')
        remaining = generate_page.remove_rutracker_pending_topics(
            pending,
            {candidates[0]['topic_id']},
        )
        self.assertEqual(remaining, [])

    def test_pending_attempt_counter_survives_repeated_failures(self):
        topic = generate_page.parse_rutracker_atom_feed(ATOM, 'nashe_kino')[0]
        pending, _ = generate_page.merge_rutracker_pending_topics(
            [],
            [topic],
            reason='detail_unavailable',
            attempted=True,
        )
        pending, added = generate_page.merge_rutracker_pending_topics(
            pending,
            [topic],
            reason='detail_unavailable',
            attempted=True,
        )

        self.assertEqual(added, 0)
        self.assertEqual(pending[0]['_pending_attempts'], 2)


if __name__ == '__main__':
    unittest.main()
