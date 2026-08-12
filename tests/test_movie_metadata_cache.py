import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import activity_collections
from movie_metadata_cache import (
    apply_cached_movie_metadata,
    invalidate_cached_movie_metadata,
    load_movie_metadata_cache,
    sync_movie_metadata_cache,
)


class MovieMetadataCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache_path = self.root / 'movie_metadata_cache.json'
        (self.root / 'posters').mkdir()
        (self.root / 'posters' / 'tt1234567.jpg').write_bytes(b'poster')

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_shares_movie_fields_but_not_torrent_fields(self):
        source = {
            'topic_id': 'source',
            'movie_title': 'Example Film',
            'movie_year': '2026',
            'imdb_id': 'tt1234567',
            'kp_id': '7654321',
            'imdb_rating': '7.8',
            'imdb_votes': '1200',
            'poster_url': 'data/posters/tt1234567.jpg',
            'youtube_url': 'https://www.youtube.com/watch?v=abcdefghijk',
            'genre': 'драма',
            'magnet': 'magnet-source',
            'format': 'mkv',
            'size_bytes': 100,
            'seeders': 50,
        }
        duplicate = {
            'topic_id': 'duplicate',
            'movie_title': 'Different Local Title',
            'movie_year': '2026',
            'imdb_id': 'tt1234567',
            'magnet': 'magnet-duplicate',
            'format': 'mp4',
            'size_bytes': 200,
            'seeders': 5,
        }

        stats = sync_movie_metadata_cache([source, duplicate], self.cache_path)

        self.assertEqual(stats['records'], 1)
        self.assertEqual(stats['topics_updated'], 1)
        self.assertEqual(duplicate['kp_id'], '7654321')
        self.assertEqual(duplicate['imdb_rating'], '7.8')
        self.assertEqual(duplicate['poster_url'], 'data/posters/tt1234567.jpg')
        self.assertEqual(duplicate['youtube_url'], source['youtube_url'])
        self.assertEqual(duplicate['genre'], 'драма')
        self.assertEqual(duplicate['magnet'], 'magnet-duplicate')
        self.assertEqual(duplicate['format'], 'mp4')
        self.assertEqual(duplicate['size_bytes'], 200)
        self.assertEqual(duplicate['seeders'], 5)

    def test_exact_title_and_year_can_link_topics_without_ids(self):
        source = {
            'movie_title': 'Район 9',
            'movie_year': '2009',
            'kp_rating': '7.9',
            'genre': 'фантастика',
        }
        duplicate = {
            'movie_title': 'Район-9',
            'movie_year': '2009',
        }

        sync_movie_metadata_cache([source, duplicate], self.cache_path)

        self.assertEqual(duplicate['kp_rating'], '7.9')
        self.assertEqual(duplicate['genre'], 'фантастика')

    def test_conflicting_ids_are_not_merged_by_title(self):
        left = {
            'movie_title': 'Одинаковое название',
            'movie_year': '2020',
            'imdb_id': 'tt1234567',
            'genre': 'драма',
        }
        right = {
            'movie_title': 'Одинаковое название',
            'movie_year': '2020',
            'imdb_id': 'tt7654321',
        }

        stats = sync_movie_metadata_cache([left, right], self.cache_path)

        self.assertEqual(stats['records'], 2)
        self.assertNotIn('genre', right)

    def test_ambiguous_title_and_year_do_not_fill_topic_without_id(self):
        left = {
            'movie_title': 'Одинаковое название',
            'movie_year': '2020',
            'imdb_id': 'tt1234567',
            'genre': 'драма',
        }
        right = {
            'movie_title': 'Одинаковое название',
            'movie_year': '2020',
            'imdb_id': 'tt7654321',
            'genre': 'комедия',
        }
        unknown = {
            'movie_title': 'Одинаковое название',
            'movie_year': '2020',
        }

        sync_movie_metadata_cache([left, right, unknown], self.cache_path)

        self.assertNotIn('imdb_id', unknown)
        self.assertNotIn('genre', unknown)

    def test_same_kp_id_does_not_link_different_titles(self):
        left = {
            'movie_title': 'Первый фильм',
            'movie_year': '2026',
            'kp_id': '42',
            'poster_url': 'data/posters/tt1234567.jpg',
            'genre': 'драма',
        }
        right = {
            'movie_title': 'Другой фильм',
            'movie_year': '2026',
            'kp_id': '42',
        }

        stats = sync_movie_metadata_cache([left, right], self.cache_path)

        self.assertEqual(stats['records'], 2)
        self.assertNotIn('poster_url', right)
        self.assertNotIn('genre', right)

    def test_different_years_are_not_merged(self):
        source = {
            'movie_title': 'Фильм',
            'movie_year': '2001',
            'genre': 'драма',
        }
        remake = {
            'movie_title': 'Фильм',
            'movie_year': '2021',
        }

        sync_movie_metadata_cache([source, remake], self.cache_path)

        self.assertNotIn('genre', remake)

    def test_existing_topic_value_is_not_replaced(self):
        source = {
            'movie_title': 'Film',
            'movie_year': '2025',
            'imdb_id': 'tt1234567',
            'genre': 'драма',
        }
        target = {
            'movie_title': 'Film',
            'movie_year': '2025',
            'imdb_id': 'tt1234567',
            'genre': 'комедия',
        }

        sync_movie_metadata_cache([source, target], self.cache_path)

        self.assertEqual(target['genre'], 'комедия')

    def test_missing_local_poster_is_not_cached(self):
        source = {
            'movie_title': 'Film',
            'movie_year': '2025',
            'imdb_id': 'tt1234567',
            'poster_url': 'data/posters/missing.jpg',
        }
        target = {'imdb_id': 'tt1234567'}

        sync_movie_metadata_cache([source, target], self.cache_path)

        self.assertNotIn('poster_url', target)

    def test_read_only_apply_uses_existing_cache(self):
        source = {
            'movie_title': 'Film',
            'movie_year': '2025',
            'imdb_id': 'tt1234567',
            'kp_id': '42',
        }
        sync_movie_metadata_cache([source], self.cache_path)
        target = {'imdb_id': 'tt1234567'}

        applied = apply_cached_movie_metadata(target, self.cache_path)
        cache = load_movie_metadata_cache(self.cache_path)

        self.assertEqual(applied, ['kp_id'])
        self.assertEqual(target['kp_id'], '42')
        self.assertEqual(len(cache['movies']), 1)

    def test_activity_collection_reuses_catalog_metadata(self):
        catalog_path = self.root / 'torrents_data.json'
        source = {
            'topic_id': 'original',
            'collection': 'nashe_kino',
            'movie_title': 'Фильм',
            'movie_year': '2025',
            'imdb_id': 'tt1234567',
            'poster_url': 'data/posters/tt1234567.jpg',
            'genre': 'драма',
            'youtube_url': 'https://www.youtube.com/watch?v=abcdefghijk',
            'magnet': 'magnet:?xt=urn:btih:' + ('a' * 40),
            'format': 'mkv',
            'size_bytes': 100,
        }
        catalog_path.write_text(json.dumps([source]), encoding='utf-8')
        activity_input = {
            'movie_title': 'Фильм',
            'movie_year': '2025',
            'imdb_id': 'tt1234567',
            'magnet': 'magnet:?xt=urn:btih:' + ('b' * 40),
            'format': 'mp4',
            'size_bytes': 200,
        }

        with (
            patch.object(activity_collections, 'CATALOG_FILE', catalog_path),
            patch.object(activity_collections, 'MOVIE_METADATA_FILE', self.cache_path),
        ):
            discovered = activity_collections.record_discovered(activity_input)

        saved = json.loads(catalog_path.read_text('utf-8'))
        self.assertEqual(discovered['poster_url'], source['poster_url'])
        self.assertEqual(discovered['genre'], 'драма')
        self.assertEqual(discovered['youtube_url'], source['youtube_url'])
        self.assertEqual(discovered['format'], 'mp4')
        self.assertEqual(discovered['size_bytes'], 200)
        self.assertEqual(len(saved), 2)

    def test_invalidated_trailer_is_not_restored_from_stale_duplicate(self):
        old_url = 'https://www.youtube.com/watch?v=abcdefghijk'
        source = {
            'movie_title': 'Film',
            'movie_year': '2025',
            'imdb_id': 'tt1234567',
            'youtube_url': old_url,
        }
        sync_movie_metadata_cache([source], self.cache_path)

        source['youtube_url'] = ''
        removed = invalidate_cached_movie_metadata(
            source,
            self.cache_path,
            ('youtube_url',),
        )
        stale_duplicate = {
            'imdb_id': 'tt1234567',
            'youtube_url': old_url,
        }
        sync_movie_metadata_cache([stale_duplicate], self.cache_path)
        empty_duplicate = {'imdb_id': 'tt1234567'}
        applied = apply_cached_movie_metadata(empty_duplicate, self.cache_path)

        self.assertEqual(removed, ['youtube_url'])
        self.assertNotIn('youtube_url', applied)
        self.assertNotIn('youtube_url', empty_duplicate)

    def test_verified_replacement_unblocks_cached_field(self):
        old_url = 'https://www.youtube.com/watch?v=abcdefghijk'
        new_url = 'https://www.youtube.com/watch?v=lmnopqrstuv'
        source = {
            'imdb_id': 'tt1234567',
            'movie_title': 'Film',
            'movie_year': '2025',
            'youtube_url': old_url,
        }
        sync_movie_metadata_cache([source], self.cache_path)
        invalidate_cached_movie_metadata(source, self.cache_path, ('youtube_url',))
        source['youtube_url'] = new_url

        sync_movie_metadata_cache(
            [source],
            self.cache_path,
            replace_fields=('youtube_url',),
        )
        target = {'imdb_id': 'tt1234567'}
        apply_cached_movie_metadata(target, self.cache_path)

        self.assertEqual(target['youtube_url'], new_url)

    def test_short_imdb_id_is_canonicalized(self):
        short = {
            'movie_title': 'The Godfather',
            'movie_year': '1972',
            'imdb_id': 'tt68646',
            'genre': 'драма',
        }
        full = {
            'movie_title': 'The Godfather',
            'movie_year': '1972',
            'imdb_id': 'tt0068646',
        }

        stats = sync_movie_metadata_cache([short, full], self.cache_path)
        cache = load_movie_metadata_cache(self.cache_path)

        self.assertEqual(stats['records'], 1)
        self.assertEqual(cache['movies'][0]['metadata']['imdb_id'], 'tt0068646')
        self.assertEqual(full['genre'], 'драма')

    def test_same_imdb_with_conflicting_kp_blocks_kp_propagation(self):
        left = {
            'movie_title': 'Film',
            'movie_year': '2026',
            'imdb_id': 'tt1234567',
            'kp_id': '100',
            'kp_rating': '7.0',
        }
        right = {
            'movie_title': 'Film',
            'movie_year': '2026',
            'imdb_id': 'tt1234567',
            'kp_id': '200',
            'kp_rating': '8.0',
        }

        stats = sync_movie_metadata_cache([left, right], self.cache_path)
        target = {'imdb_id': 'tt1234567'}
        apply_cached_movie_metadata(target, self.cache_path)
        cache = load_movie_metadata_cache(self.cache_path)

        self.assertEqual(stats['records'], 1)
        self.assertNotIn('kp_id', target)
        self.assertNotIn('kp_rating', target)
        self.assertIn('kp_id', cache['movies'][0]['blocked_fields'])
        self.assertIn('kp_rating', cache['movies'][0]['blocked_fields'])

    def test_repeated_sync_does_not_grow_ambiguous_records(self):
        topics = [
            {
                'topic_id': 'left',
                'movie_title': 'Same title',
                'movie_year': '2020',
                'imdb_id': 'tt1234567',
            },
            {
                'topic_id': 'right',
                'movie_title': 'Same title',
                'movie_year': '2020',
                'imdb_id': 'tt7654321',
            },
            {
                'topic_id': 'unknown',
                'movie_title': 'Same title',
                'movie_year': '2020',
            },
        ]

        first = sync_movie_metadata_cache(topics, self.cache_path)
        second = sync_movie_metadata_cache(topics, self.cache_path)

        self.assertEqual(first['records'], 3)
        self.assertEqual(second['records'], 3)
        self.assertEqual(second['records_created'], 0)


if __name__ == '__main__':
    unittest.main()
