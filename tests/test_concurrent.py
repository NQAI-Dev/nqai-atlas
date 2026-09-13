"""Concurrent-write safety: append_record must serialize via flock."""
from __future__ import annotations
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import atlas


def make_record(text: str) -> dict:
    return {
        "id": f"rec_{text}", "ts": "2026-09-13T00:00:00Z", "kind": "fact",
        "entity": "project:race", "text": text, "status": "active",
        "source": {"kind": "test", "ref": "x"}, "confidence": 1.0,
        "tags": [], "supersedes": None, "relations": [],
    }


class ConcurrentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records = Path(self.tmp.name) / "records.jsonl"
        self._patch = patch.object(atlas, "RECORDS", self.records)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_threads_produce_valid_jsonl(self):
        n = 32
        threads = [threading.Thread(target=atlas.append_record, args=(make_record(f"t{i}"),)) for i in range(n)]
        for t in threads: t.start()
        for t in threads: t.join()

        lines = self.records.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), n, f"expected {n} lines, got {len(lines)}")
        for line in lines:
            record = json.loads(line)  # raises if any thread interleaved mid-write
            self.assertEqual(record["kind"], "fact")
            self.assertTrue(record["id"].startswith("rec_t"))

    def test_verify_after_concurrent_writes(self):
        n = 16
        threads = [threading.Thread(target=atlas.append_record, args=(make_record(f"v{i}"),)) for i in range(n)]
        for t in threads: t.start()
        for t in threads: t.join()

        # Empty SimpleNamespace satisfies verify's signature.
        self.assertEqual(atlas.verify(type("Args", (), {})()), 0)


if __name__ == "__main__":
    unittest.main()
