"""Tests for suggest_next() and the atlas_suggest MCP tool."""
from __future__ import annotations

import json
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import atlas
import mcp_server


def _ts(days_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _record(entity: str, kind: str, text: str, tags: list | None = None,
            status: str = "active", ts: str | None = None,
            rec_id: str | None = None, source: dict | None = None) -> dict:
    return {
        "id": rec_id or f"rec_{entity.replace(':', '_')}_{kind}",
        "ts": ts or _ts(0),
        "kind": kind,
        "entity": entity,
        "text": text,
        "status": status,
        "source": source or {"kind": "test", "ref": "test"},
        "confidence": 1.0,
        "tags": tags or [],
        "supersedes": None,
        "relations": [],
    }


class SuggestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records_path = Path(self.tmp.name) / "records.jsonl"
        self.patch = patch.object(atlas, "RECORDS", self.records_path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _write(self, records: list[dict]) -> None:
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        self.records_path.write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
        )

    def test_empty_store_returns_empty_list(self):
        result = atlas.suggest_next()
        self.assertEqual(result, [])

    def test_stalled_goal_surfaced(self):
        self._write([_record("project:myapp", "goal", "ship v2", ts=_ts(20))])
        result = atlas.suggest_next()
        entities = [s["entity"] for s in result]
        self.assertIn("project:myapp", entities)
        s = next(x for x in result if x["entity"] == "project:myapp")
        self.assertEqual(s["action"], "review-goal")

    def test_recent_goal_not_surfaced(self):
        self._write([_record("project:myapp", "goal", "ship v2", ts=_ts(3))])
        result = atlas.suggest_next()
        goal_suggestions = [s for s in result if s["entity"] == "project:myapp"
                            and s["action"] == "review-goal"]
        self.assertEqual(goal_suggestions, [])

    def test_stale_decision_surfaced(self):
        self._write([_record("project:myapp", "decision", "use postgres",
                             ts=_ts(25), rec_id="dec-old")])
        result = atlas.suggest_next()
        decision_suggestions = [s for s in result if s["action"] == "review-decision"]
        self.assertTrue(decision_suggestions)

    def test_fresh_decision_not_surfaced(self):
        self._write([_record("project:myapp", "decision", "use postgres",
                             ts=_ts(5), rec_id="dec-fresh")])
        result = atlas.suggest_next()
        decision_suggestions = [s for s in result if s["action"] == "review-decision"]
        self.assertEqual(decision_suggestions, [])

    def test_stale_health_check_surfaced(self):
        stale_ts = _ts(3)
        payload = json.dumps({"status": "healthy", "checked_at": stale_ts})
        self._write([_record(
            "service:web", "observation", payload,
            tags=["health-check", "healthy"],
            source={"kind": "health-check", "ref": "https://web/health"},
        )])
        result = atlas.suggest_next()
        hc = [s for s in result if s["action"] == "re-check-health"
              and s["entity"] == "service:web"]
        self.assertTrue(hc)

    def test_fresh_health_check_not_surfaced(self):
        fresh_ts = _ts(0)
        payload = json.dumps({"status": "healthy", "checked_at": fresh_ts})
        self._write([_record(
            "service:web", "observation", payload,
            tags=["health-check", "healthy"],
            source={"kind": "health-check", "ref": "https://web/health"},
        )])
        result = atlas.suggest_next()
        hc = [s for s in result if s["action"] == "re-check-health"
              and s["entity"] == "service:web"]
        self.assertEqual(hc, [])

    def test_dirty_git_snapshot_surfaced(self):
        snapshot = json.dumps({"name": "nodepulse",
                               "path": "/home/openclaw/Projects/nodepulse",
                               "branch": "main", "dirty": True, "changed": 3,
                               "last_commit": "abc fix"})
        self._write([_record(
            "project:nodepulse", "observation", snapshot,
            tags=["git", "inventory"],
            source={"kind": "filesystem", "ref": "/home/openclaw/Projects/nodepulse"},
        )])
        result = atlas.suggest_next()
        dirty = [s for s in result if s["action"] == "commit-dirty-work"
                 and s["entity"] == "project:nodepulse"]
        self.assertTrue(dirty)

    def test_clean_git_snapshot_not_surfaced(self):
        snapshot = json.dumps({"name": "nqai-atlas",
                               "path": "/home/openclaw/Projects/nqai-atlas",
                               "branch": "main", "dirty": False, "changed": 0,
                               "last_commit": "abc clean"})
        self._write([_record(
            "project:nqai-atlas", "observation", snapshot,
            tags=["git", "inventory"],
            source={"kind": "filesystem", "ref": "/home/openclaw/Projects/nqai-atlas"},
        )])
        result = atlas.suggest_next()
        dirty = [s for s in result if s["action"] == "commit-dirty-work"]
        self.assertEqual(dirty, [])

    def test_suggestion_schema(self):
        self._write([_record("project:x", "goal", "Ship feature Y", ts=_ts(20))])
        result = atlas.suggest_next()
        for s in result:
            self.assertIn("entity", s)
            self.assertIn("action", s)
            self.assertIn("reason", s)
            self.assertIn("priority", s)
            self.assertIn("record_id", s)
            self.assertIn(s["priority"], {"high", "medium", "low"})

    def test_deduplication_one_per_entity_per_action(self):
        # Three stale goals for the same entity → only one review-goal suggestion
        self._write([
            _record("project:nodepulse", "goal", f"Goal {i}",
                    ts=_ts(20 + i), rec_id=f"g{i}")
            for i in range(3)
        ])
        result = atlas.suggest_next()
        review = [s for s in result if s["entity"] == "project:nodepulse"
                  and s["action"] == "review-goal"]
        self.assertEqual(len(review), 1)

    def test_entity_filter(self):
        self._write([
            _record("project:a", "goal", "goal a", ts=_ts(20), rec_id="ga"),
            _record("project:b", "goal", "goal b", ts=_ts(20), rec_id="gb"),
        ])
        result = atlas.suggest_next(entity="project:a")
        entities = {s["entity"] for s in result}
        self.assertIn("project:a", entities)
        self.assertNotIn("project:b", entities)

    def test_high_goal_age_is_high_priority(self):
        # Goal > 2× stale threshold → high priority
        self._write([_record("project:x", "goal", "ancient goal", ts=_ts(30))])
        result = atlas.suggest_next()
        s = next((x for x in result if x["entity"] == "project:x"), None)
        self.assertIsNotNone(s)
        self.assertEqual(s["priority"], "high")

    def test_medium_goal_age_is_medium_priority(self):
        # Goal between 14–28 days → medium priority
        self._write([_record("project:x", "goal", "mid goal", ts=_ts(16))])
        result = atlas.suggest_next()
        s = next((x for x in result if x["entity"] == "project:x"), None)
        self.assertIsNotNone(s)
        self.assertEqual(s["priority"], "medium")


class SuggestCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records_path = Path(self.tmp.name) / "records.jsonl"
        self.patch = patch.object(atlas, "RECORDS", self.records_path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_cli_empty_exits_zero(self):
        import argparse
        args = argparse.Namespace(entity=None)
        code = atlas.suggest(args)
        self.assertEqual(code, 0)

    def test_cli_prints_suggestions(self):
        import argparse
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        self.records_path.write_text(json.dumps(_record(
            "project:test", "goal", "test goal", ts=_ts(20)
        )) + "\n", encoding="utf-8")
        import io
        import sys
        captured = io.StringIO()
        sys.stdout = captured
        try:
            args = argparse.Namespace(entity=None)
            code = atlas.suggest(args)
        finally:
            sys.stdout = sys.__stdout__
        self.assertEqual(code, 0)
        self.assertIn("project:test", captured.getvalue())


class SuggestMCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records_path = Path(self.tmp.name) / "records.jsonl"
        self.atlas_patch = patch.object(atlas, "RECORDS", self.records_path)
        self.atlas_patch.start()

    def tearDown(self):
        self.atlas_patch.stop()
        self.tmp.cleanup()

    def _call(self, args: dict) -> dict:
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "atlas_suggest", "arguments": args}}
        return mcp_server.handle(req)

    def test_mcp_empty_store(self):
        resp = self._call({})
        self.assertFalse(resp["result"]["isError"])
        text = resp["result"]["content"][0]["text"]
        self.assertIn("No suggestions", text)

    def test_mcp_returns_suggestions(self):
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        self.records_path.write_text(json.dumps(_record(
            "project:test", "goal", "MCP goal", ts=_ts(25)
        )) + "\n", encoding="utf-8")
        resp = self._call({})
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("project:test", resp["result"]["content"][0]["text"])

    def test_mcp_entity_filter(self):
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        self.records_path.write_text(
            json.dumps(_record("project:a", "goal", "goal a", ts=_ts(20))) + "\n" +
            json.dumps(_record("project:b", "goal", "goal b", ts=_ts(20))) + "\n",
            encoding="utf-8"
        )
        resp = self._call({"entity": "project:a"})
        text = resp["result"]["content"][0]["text"]
        self.assertIn("project:a", text)
        self.assertNotIn("project:b", text)

    def test_tools_list_includes_suggest(self):
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        resp = mcp_server.handle(req)
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertIn("atlas_suggest", names)
