import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import atlas


class AtlasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records = Path(self.tmp.name) / "records.jsonl"
        self.patch = patch.object(atlas, "RECORDS", self.records)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_add_and_explain_preserves_history(self):
        atlas.add(SimpleNamespace(
            kind="decision", entity="atlas", text="first", status="active",
            source="test", confidence=1.0, tag=["one"], supersedes=None,
        ))
        first = atlas.records()[0]
        first["status"] = "superseded"
        self.records.write_text(json.dumps(first) + "\n", encoding="utf-8")
        atlas.add(SimpleNamespace(
            kind="decision", entity="atlas", text="second", status="active",
            source="test", confidence=1.0, tag=[], supersedes=first["id"],
        ))
        explanation = atlas.explain_entity("atlas")
        self.assertIn("CURRENT", explanation)
        self.assertIn("<- " + first["id"], explanation)

    def test_verify_rejects_unknown_supersedes(self):
        self.records.write_text(json.dumps({
            "id": "new", "ts": "2026-09-12T00:00:00Z", "kind": "decision",
            "entity": "atlas", "text": "broken", "status": "active",
            "source": {"kind": "test", "ref": "x"}, "confidence": 1,
            "tags": [], "supersedes": "missing",
        }) + "\n", encoding="utf-8")
        self.assertEqual(atlas.verify(SimpleNamespace()), 1)


if __name__ == "__main__":
    unittest.main()
