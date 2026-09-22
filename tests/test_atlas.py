import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import atlas

class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_bootstrap_creates_missing_store_from_seed(self):
        seed = Path(self.tmp.name) / "seed.jsonl"
        seed.write_text(json.dumps({"id": "s1", "ts": "2026-09-18T00:00:00Z",
                                    "kind": "decision", "entity": "nqai-atlas",
                                    "text": "seeded", "status": "active",
                                    "source": {"kind": "conversation", "ref": "seed"},
                                    "confidence": 1.0, "tags": [], "supersedes": None,
                                    "relations": []}) + "\n", encoding="utf-8")
        target = Path(self.tmp.name) / "records.jsonl"
        self.assertTrue(atlas.bootstrap_store(target, seed))
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), seed.read_text(encoding="utf-8"))

    def test_bootstrap_skips_existing_store(self):
        seed = Path(self.tmp.name) / "seed.jsonl"
        seed.write_text("", encoding="utf-8")
        target = Path(self.tmp.name) / "records.jsonl"
        target.write_text("existing\n", encoding="utf-8")
        self.assertFalse(atlas.bootstrap_store(target, seed))
        self.assertEqual(target.read_text(encoding="utf-8"), "existing\n")

    def test_bootstrap_without_seed_is_noop(self):
        target = Path(self.tmp.name) / "records.jsonl"
        self.assertFalse(atlas.bootstrap_store(target, Path(self.tmp.name) / "absent.jsonl"))
        self.assertFalse(target.exists())


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


    def test_history_shows_all_records_chronologically(self):
        first = {
            "id": "rec-old", "ts": "2026-09-18T10:00:00Z", "kind": "decision",
            "entity": "project:atlas", "text": "old claim", "status": "superseded",
            "source": {"kind": "test", "ref": "one"}, "confidence": 1.0,
            "tags": [], "supersedes": None, "relations": [],
        }
        second = {
            "id": "rec-new", "ts": "2026-09-18T11:00:00Z", "kind": "decision",
            "entity": "project:atlas", "text": "new claim", "status": "active",
            "source": {"kind": "test", "ref": "two"}, "confidence": 1.0,
            "tags": [], "supersedes": "rec-old", "relations": [],
        }
        archived = {
            "id": "rec-archived", "ts": "2026-09-18T12:00:00Z", "kind": "fact",
            "entity": "project:atlas", "text": "archived note", "status": "archived",
            "source": {"kind": "test", "ref": "three"}, "confidence": 1.0,
            "tags": [], "supersedes": None, "relations": [],
        }
        for record in (second, archived, first):
            atlas.append_record(record)

        history = atlas.history_entity("project:atlas")
        self.assertIn("Records: 3", history)
        self.assertLess(history.index("old claim"), history.index("new claim"))
        self.assertIn("superseded_by=rec-new", history)
        self.assertIn("supersedes=rec-old", history)
        self.assertIn("archived note", history)
        self.assertEqual(atlas.history_entity("missing"), "No history for entity: missing")

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

    def test_health_checks_supersede_only_same_source(self):
        first = atlas.record_health_check(
            "service:web", "healthy", "https://web/health", "2026-09-18T20:00:00Z",
        )
        other = atlas.record_health_check(
            "service:web", "healthy", "systemd:web", "2026-09-18T20:01:00Z",
        )
        latest = atlas.record_health_check(
            "service:web", "degraded", "https://web/health", "2026-09-18T20:02:00Z", "latency high",
        )

        self.assertEqual(latest["supersedes"], first["id"])
        self.assertIsNone(other["supersedes"])
        current = atlas.current_records("service:web")
        self.assertEqual({item["source"]["ref"] for item in current}, {"https://web/health", "systemd:web"})
        payload = json.loads(next(item["text"] for item in current if item["id"] == latest["id"]))
        self.assertEqual(payload, {
            "checked_at": "2026-09-18T20:02:00Z",
            "detail": "latency high",
            "status": "degraded",
        })

    def test_health_check_rejects_invalid_status_and_timestamp(self):
        with self.assertRaisesRegex(ValueError, "health status"):
            atlas.record_health_check("service:web", "broken", "probe")
        with self.assertRaisesRegex(ValueError, "ISO-8601"):
            atlas.record_health_check("service:web", "healthy", "probe", "not-a-time")
        with self.assertRaisesRegex(ValueError, "timezone"):
            atlas.record_health_check("service:web", "healthy", "probe", "2026-09-18T20:00:00")

    def test_verify_rejects_non_object_record(self):
        self.records.write_text('[]\n', encoding="utf-8")
        self.assertEqual(atlas.verify(SimpleNamespace()), 1)

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

    def test_verify_rejects_invalid_record_field_types(self):
        self.records.write_text(json.dumps({
            "id": "bad", "ts": "2026-09-12T00:00:00Z", "kind": "fact",
            "entity": "project:test", "text": "", "status": "active",
            "source": {"kind": "test", "ref": "x"}, "confidence": 2,
            "tags": ["ok", 7], "supersedes": None, "relations": [],
        }) + "\n", encoding="utf-8")
        self.assertEqual(atlas.verify(SimpleNamespace()), 1)

    def test_verify_rejects_malformed_timestamp_without_crashing(self):
        self.records.write_text(json.dumps({
            "id": "bad", "ts": {"not": "a timestamp"}, "kind": "fact",
            "entity": "project:test", "text": "ok", "status": "active",
            "source": {"kind": "test", "ref": "x"}, "confidence": 1,
            "tags": [], "supersedes": None, "relations": [],
        }) + "\n", encoding="utf-8")
        self.assertEqual(atlas.verify(SimpleNamespace()), 1)

    def test_verify_rejects_malformed_source_and_relation_shape(self):
        self.records.write_text(json.dumps({
            "id": "bad", "ts": "2026-09-12T00:00:00Z", "kind": "fact",
            "entity": "project:test", "text": "ok", "status": "active",
            "source": "test", "confidence": 1, "tags": [], "supersedes": None,
            "relations": [{"type": "relates_to"}],
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
