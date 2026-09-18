import json
import subprocess
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

    def test_verify_rejects_invalid_relation(self):
        self.records.write_text(json.dumps({
            "id": "bad", "ts": "2026-09-12T00:00:00Z", "kind": "fact",
            "entity": "project:test", "text": "bad", "status": "active",
            "source": {"kind": "test", "ref": "x"}, "confidence": 1,
            "tags": [], "supersedes": None,
            "relations": [{"type": "invented", "entity": "host:x"}],
        }) + "\n", encoding="utf-8")
        self.assertEqual(atlas.verify(SimpleNamespace()), 1)

    def test_verify_rejects_unknown_supersedes(self):
        self.records.write_text(json.dumps({
            "id": "new", "ts": "2026-09-12T00:00:00Z", "kind": "decision",
            "entity": "atlas", "text": "broken", "status": "active",
            "source": {"kind": "test", "ref": "x"}, "confidence": 1,
            "tags": [], "supersedes": "missing",
        }) + "\n", encoding="utf-8")
        self.assertEqual(atlas.verify(SimpleNamespace()), 1)

    def test_project_observations_skip_unchanged_and_supersede_changes(self):
        root = Path(self.tmp.name) / "projects"
        project = root / "demo"
        project.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "Atlas Test"], cwd=project, check=True)
        (project / "README.md").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=project, check=True)

        self.assertEqual(atlas.record_project_observations(root), (1, 0))
        first = atlas.records()[0]
        self.assertEqual(atlas.record_project_observations(root), (0, 1))
        self.assertEqual(len(atlas.records()), 1)

        (project / "README.md").write_text("two\n", encoding="utf-8")
        self.assertEqual(atlas.record_project_observations(root), (1, 0))
        second = atlas.records()[-1]
        self.assertEqual(second["supersedes"], first["id"])
        self.assertTrue(json.loads(second["text"])["dirty"])

    def test_observe_projects_does_not_supersede_other_record_kinds(self):
        atlas.append_record({
            "id": "fact-1", "ts": "2026-09-18T00:00:00Z", "kind": "fact",
            "entity": "project:demo", "text": "durable fact", "status": "active",
            "source": {"kind": "test", "ref": "x"}, "confidence": 1.0,
            "tags": [], "supersedes": None, "relations": [],
        })
        projects = Path(self.tmp.name) / "projects"
        project = projects / "demo"
        (project / ".git").mkdir(parents=True)

        with patch.object(atlas, "project_inventory", return_value=[{
            "name": "demo", "path": str(project), "branch": "main",
            "dirty": False, "changed": 0, "last_commit": "abc initial",
        }]):
            self.assertEqual(atlas.observe_projects(SimpleNamespace(root=str(projects))), 0)

        self.assertIsNone(atlas.records()[-1]["supersedes"])


if __name__ == "__main__":
    unittest.main()
