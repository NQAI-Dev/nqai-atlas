"""End-to-end CLI tests run against an isolated temp records.jsonl."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

def run_cli(records: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ATLAS_RECORDS": str(records)}
    return subprocess.run(
        [sys.executable, str(REPO / "atlas.py"), *args],
        capture_output=True, text=True, env=env, check=False,
    )


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records = Path(self.tmp.name) / "records.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_prints_id_and_persists(self):
        proc = run_cli(self.records, "add", "--kind", "fact", "--entity", "project:test",
                       "--text", "hello", "--source", "test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record_id = proc.stdout.strip()
        self.assertTrue(record_id.startswith("rec_"))
        lines = self.records.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["id"], record_id)
        self.assertEqual(record["kind"], "fact")
        self.assertEqual(record["entity"], "project:test")

    def test_health_check_records_source_timestamp_and_history(self):
        first = run_cli(
            self.records, "health-check", "--entity", "service:web", "--status", "healthy",
            "--source", "https://web/health", "--checked-at", "2026-09-18T20:00:00Z",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run_cli(
            self.records, "health-check", "--entity", "service:web", "--status", "unhealthy",
            "--source", "https://web/health", "--checked-at", "2026-09-18T20:05:00Z",
            "--detail", "HTTP 503",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        records = [json.loads(line) for line in self.records.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["supersedes"], records[0]["id"])
        self.assertEqual(records[-1]["source"], {"kind": "health-check", "ref": "https://web/health"})
        self.assertEqual(json.loads(records[-1]["text"])["checked_at"], "2026-09-18T20:05:00Z")

    def test_add_rejects_invalid_kind(self):
        proc = run_cli(self.records, "add", "--kind", "bogus", "--entity", "project:test",
                       "--text", "x", "--source", "test")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid kind", proc.stderr)
        self.assertFalse(self.records.exists())

    def test_search_filters_by_entity_and_kind(self):
        run_cli(self.records, "add", "--kind", "fact", "--entity", "project:a",
                "--text", "alpha", "--source", "test")
        run_cli(self.records, "add", "--kind", "decision", "--entity", "project:a",
                "--text", "beta", "--source", "test")
        run_cli(self.records, "add", "--kind", "fact", "--entity", "project:b",
                "--text", "gamma", "--source", "test")

        proc = run_cli(self.records, "search", "--entity", "project:a", "--kind", "fact")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("alpha", proc.stdout)
        self.assertNotIn("beta", proc.stdout)
        self.assertNotIn("gamma", proc.stdout)

        proc = run_cli(self.records, "search", "--text", "gam")
        self.assertIn("gamma", proc.stdout)

    def test_explain_shows_supersedes_chain(self):
        first_id = run_cli(self.records, "add", "--kind", "decision", "--entity", "atlas",
                           "--text", "first", "--source", "test").stdout.strip()
        # Mark first as superseded manually, then add second.
        record = json.loads(self.records.read_text(encoding="utf-8").splitlines()[0])
        record["status"] = "superseded"
        self.records.write_text(json.dumps(record) + "\n", encoding="utf-8")
        run_cli(self.records, "add", "--kind", "decision", "--entity", "atlas",
                "--text", "second", "--source", "test", "--supersedes", first_id)

        proc = run_cli(self.records, "explain", "--entity", "atlas")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CURRENT", proc.stdout)
        self.assertIn(first_id, proc.stdout)

    def test_context_returns_related(self):
        run_cli(self.records, "add", "--kind", "fact", "--entity", "service:x",
                "--text", "x runs", "--source", "test")
        # host:h1 points at service:x, so context service:x finds host:h1 as related.
        run_cli(self.records, "add", "--kind", "fact", "--entity", "host:h1",
                "--text", "h1 fact", "--source", "test",
                "--relation", "service:x")
        proc = run_cli(self.records, "context", "--entity", "service:x")
        self.assertIn("RELATED", proc.stdout)
        self.assertIn("h1 fact", proc.stdout)

    def test_verify_clean_store_passes(self):
        run_cli(self.records, "add", "--kind", "fact", "--entity", "project:test",
                "--text", "ok", "--source", "test")
        proc = run_cli(self.records, "verify")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("valid records", proc.stdout)

    def test_history_shows_superseded_and_archived_records(self):
        first_id = run_cli(self.records, "add", "--kind", "decision", "--entity", "atlas",
                           "--text", "first", "--source", "test").stdout.strip()
        run_cli(self.records, "add", "--kind", "decision", "--entity", "atlas",
                "--text", "second", "--source", "test", "--supersedes", first_id)
        records = [json.loads(line) for line in self.records.read_text(encoding="utf-8").splitlines()]
        records[0]["status"] = "superseded"
        records[1]["status"] = "archived"
        self.records.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

        proc = run_cli(self.records, "history", "--entity", "atlas")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Records: 2", proc.stdout)
        self.assertIn("superseded_by=", proc.stdout)
        self.assertIn("archived", proc.stdout)

    def test_verify_empty_store_passes(self):
        proc = run_cli(self.records, "verify")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("valid records: 0", proc.stdout)


if __name__ == "__main__":
    unittest.main()
