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


if __name__ == "__main__":
    unittest.main()
