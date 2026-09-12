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

    def test_current_records_hide_old_observations(self):
        for text in ("old", "new"):
            atlas.append_record({
                "id": atlas.new_id(), "ts": "2026-09-12T00:00:00Z", "kind": "observation",
                "entity": "project:test", "text": text, "status": "active",
                "source": {"kind": "test", "ref": "x"}, "confidence": 1.0,
                "tags": [], "supersedes": None, "relations": [],
            })
        items = atlas.records()
        items[-1]["supersedes"] = items[-2]["id"]
        self.records.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
        current = atlas.current_records("project:test")
        self.assertEqual([item["text"] for item in current], ["new"])

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
