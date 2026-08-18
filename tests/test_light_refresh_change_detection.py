import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import stream_server


class LightRefreshChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.live = root / "live"
        self.staging = root / "staging"
        for base in (self.live, self.staging):
            (base / "posters").mkdir(parents=True)
            (base / "torrents_data.json").write_text(
                json.dumps([{"topic_id": "1", "title": "Movie"}]),
                encoding="utf-8",
            )
            (base / "hidden_topics.json").write_text("[]", encoding="utf-8")
            (base / "index-kino.html").write_text("<html>same</html>", encoding="utf-8")
            (base / "posters" / "1.jpg").write_bytes(b"poster")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unchanged_catalog_is_not_marked_changed(self):
        with patch.object(stream_server, "DATA_DIR", self.live):
            self.assertFalse(
                stream_server._light_refresh_catalog_changed(self.staging)
            )

    def test_topic_change_is_detected(self):
        (self.staging / "torrents_data.json").write_text(
            json.dumps([{"topic_id": "1", "title": "Changed"}]),
            encoding="utf-8",
        )
        with patch.object(stream_server, "DATA_DIR", self.live):
            self.assertTrue(
                stream_server._light_refresh_catalog_changed(self.staging)
            )

    def test_poster_change_is_detected(self):
        shutil.copy2(
            self.live / "posters" / "1.jpg",
            self.staging / "posters" / "1.jpg",
        )
        (self.staging / "posters" / "1.jpg").write_bytes(b"new poster")
        with patch.object(stream_server, "DATA_DIR", self.live):
            self.assertTrue(
                stream_server._light_refresh_catalog_changed(self.staging)
            )

    def test_refresh_support_state_is_copied_and_published(self):
        pending_name = "rutracker_pending_topics.json"
        metadata_name = "movie_metadata_cache.json"
        (self.live / pending_name).write_text(
            json.dumps({"items": [{"topic_id": "old"}]}),
            encoding="utf-8",
        )
        (self.live / metadata_name).write_text(
            json.dumps({"movies": [{"keys": ["imdb:tt1"]}]}),
            encoding="utf-8",
        )

        with patch.object(stream_server, "DATA_DIR", self.live):
            stream_server._copy_existing_refresh_data(self.staging)

        self.assertTrue((self.staging / pending_name).exists())
        self.assertTrue((self.staging / metadata_name).exists())

        pending = {"items": [{"topic_id": "new"}]}
        metadata = {"movies": [{"keys": ["imdb:tt2"]}]}
        (self.staging / pending_name).write_text(json.dumps(pending), encoding="utf-8")
        (self.staging / metadata_name).write_text(json.dumps(metadata), encoding="utf-8")

        with patch.object(stream_server, "DATA_DIR", self.live):
            stream_server._publish_staging_refresh(self.staging)

        self.assertEqual(
            json.loads((self.live / pending_name).read_text("utf-8")),
            pending,
        )
        self.assertEqual(
            json.loads((self.live / metadata_name).read_text("utf-8")),
            metadata,
        )

    def test_refresh_input_file_is_not_published_back(self):
        manual_name = "kp_manual_ids.json"
        original = {"Film": "1"}
        (self.live / manual_name).write_text(json.dumps(original), encoding="utf-8")

        with patch.object(stream_server, "DATA_DIR", self.live):
            stream_server._copy_existing_refresh_data(self.staging)

        self.assertTrue((self.staging / manual_name).exists())
        (self.staging / manual_name).write_text(
            json.dumps({"Film": "2"}),
            encoding="utf-8",
        )

        with patch.object(stream_server, "DATA_DIR", self.live):
            stream_server._publish_staging_refresh(self.staging)

        self.assertEqual(
            json.loads((self.live / manual_name).read_text("utf-8")),
            original,
        )


if __name__ == "__main__":
    unittest.main()
