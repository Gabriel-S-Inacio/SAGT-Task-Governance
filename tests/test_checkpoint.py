import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bitrix24_task_auditor.checkpoint import CheckpointError, CheckpointStore


class CheckpointTests(unittest.TestCase):
    def test_resume_complete_and_watermark(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            store = CheckpointStore(root / "checkpoint.json", root / "tasks.ndjson")
            active = store.begin("abc", incremental=False, overlap_minutes=5)
            self.assertEqual(active["next"], 0)
            store.append_page([{"id": "1"}], 50)
            resumed = store.begin("abc", incremental=False, overlap_minutes=5)
            self.assertEqual(resumed["next"], 50)
            store.append_page([{"id": "2"}], None)
            self.assertEqual(len(store.read_spool()), 2)
            watermark = datetime(2026, 1, 1, tzinfo=timezone.utc)
            store.complete(watermark)
            self.assertFalse(store.spool_path.exists())
            self.assertEqual(store.read()["completed_watermark"], watermark.isoformat())

    def test_mismatched_resume_is_refused(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            store = CheckpointStore(root / "c.json", root / "s.ndjson")
            store.begin("one", incremental=False, overlap_minutes=0)
            with self.assertRaises(CheckpointError):
                store.begin("two", incremental=False, overlap_minutes=0)


if __name__ == "__main__":
    unittest.main()
