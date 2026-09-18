"""Tests for atlas.suggest_next() and the atlas_suggest MCP tool."""
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import atlas
import mcp_server


def _ts(delta_days: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=delta_days)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _record(entity: str, kind: str, text: str, tags: list | None = None,
            status: str = "active", ts: str | None = None, rec_id: str | None = None) -> dict:
    safe_id = entity.replace(":", "_")
    return {
        "id": rec_id or f"rec_{safe_id}_{kind}",
        "ts": ts or _ts(0),
        "kind": kind,
        "entity": entity,
        "text": text,
        "status": status,
        "source": {"kind": "test", "ref": "test"},
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

    def test_stale_goal_surfaced(self):
        # Goal older than _GOAL_STALE_DAYS with no update
        self._write([_record("project:myapp", "goal", "ship v2",
                              ts=_ts(atlas._GOAL_STALE_DAYS + 5))])
        result = atlas.suggest_next()
        self.assertTrue(any(s["action"] == "review-goal" and s["entity"] == "project:myapp"
                            for s in result))

    def test_fresh_goal_not_surfaced(self):
        self._write([_record("project:myapp", "goal", "ship v2", ts=_ts(1))])
        result = atlas.suggest_next()
        self.assertFalse(any(s["action"] == "review-goal" for s in result))

    def test_stale_decision_surfaced(self):
        self._write([_record("project:myapp", "decision", "use postgres",
                              ts=_ts(atlas._DECISION_STALE_DAYS + 5), rec_id="dec-old")])
        result = atlas.suggest_next()
        self.assertTrue(any(s["action"] == "review-decision" and s["entity"] == "project:myapp"
                            for s in result))

    def test_fresh_decision_not_surfaced(self):
        self._write([_record("project:myapp", "decision", "use postgres",
                              ts=_ts(1), rec_id="dec-fresh")])
        result = atlas.suggest_next()
        self.assertFalse(any(s["action"] == "review-decision" for s in result))

    def test_stale_health_check_surfaced(self):
        checked_at = _ts(atlas._HEALTH_STALE_DAYS + 1)
        self._write([_record(
            "service:web", "observation",
            json.dumps({"status": "healthy", "checked_at": checked_at}),
            tags=["health-check", "healthy"],
            ts=_ts(atlas._HEALTH_STALE_DAYS + 1),
        )])
        result = atlas.suggest_next()
        self.assertTrue(any(s["action"] == "re-check-health" and s["entity"] == "service:web"
                            for s in result))

    def test_recent_health_check_not_surfaced(self):
        self._write([_record(
            "service:web", "observation",
            json.dumps({"status": "healthy", "checked_at": _ts(0)}),
            tags=["health-check", "healthy"],
        )])
        result = atlas.suggest_next()
        self.assertFalse(any(s["action"] == "re-check-health" for s in result))

    def test_dirty_git_snapshot_surfaced(self):
        self._write([_record(
            "project:myapp", "observation",
            json.dumps({"dirty": True, "changed": 3, "branch": "main",
                        "name": "myapp", "path": "/p", "last_commit": "abc init"}),
            tags=["git", "inventory"],
        )])
        result = atlas.suggest_next()
        self.assertTrue(any(s["action"] == "commit-dirty-work" for s in result))

    def test_clean_git_snapshot_not_surfaced(self):
        self._write([_record(
            "project:myapp", "observation",
            json.dumps({"dirty": False, "changed": 0, "branch": "main",
                        "name": "myapp", "path": "/p", "last_commit": "abc init"}),
            tags=["git", "inventory"],
        )])
        result = atlas.suggest_next()
        self.assertFalse(any(s["action"] == "commit-dirty-work" for s in result))

    def test_high_priority_goal_comes_first(self):
        # Very stale goal should be high priority
        self._write([
            _record("project:a", "goal", "stale goal",
                    ts=_ts(atlas._GOAL_STALE_DAYS * 3), rec_id="goal-a"),
            _record("project:b", "decision", "stale decision",
                    ts=_ts(atlas._DECISION_STALE_DAYS + 5), rec_id="dec-b"),
        ])
        result = atlas.suggest_next()
        priorities = [s["priority"] for s in result]
        self.assertIn("high", priorities)
        # High must appear before medium
        high_idx = priorities.index("high")
        medium_idx = next((i for i, p in enumerate(priorities) if p == "medium"), len(priorities))
        self.assertLess(high_idx, medium_idx)

    def test_entity_filter(self):
        self._write([
            _record("project:a", "goal", "goal a", ts=_ts(atlas._GOAL_STALE_DAYS + 5), rec_id="ga"),
            _record("project:b", "goal", "goal b", ts=_ts(atlas._GOAL_STALE_DAYS + 5), rec_id="gb"),
        ])
        result = atlas.suggest_next(entity="project:a")
        self.assertTrue(all(s["entity"] == "project:a" for s in result))

    def test_suggestion_has_required_keys(self):
        self._write([_record("project:myapp", "goal", "ship v2",
                              ts=_ts(atlas._GOAL_STALE_DAYS + 5))])
        result = atlas.suggest_next()
        self.assertTrue(result)
        for s in result:
            self.assertIn("entity", s)
            self.assertIn("action", s)
            self.assertIn("reason", s)
            self.assertIn("priority", s)
            self.assertIn("record_id", s)

    def test_no_duplicate_suggestions_for_same_entity_action(self):
        # Two goal records for the same entity — should produce only one review-goal
        self._write([
            _record("project:myapp", "goal", "goal one",
                    ts=_ts(atlas._GOAL_STALE_DAYS + 5), rec_id="g1"),
            _record("project:myapp", "goal", "goal two",
                    ts=_ts(atlas._GOAL_STALE_DAYS + 5), rec_id="g2"),
        ])
        result = atlas.suggest_next()
        review_goals = [s for s in result if s["action"] == "review-goal"
                        and s["entity"] == "project:myapp"]
        self.assertEqual(len(review_goals), 1)


class SuggestMCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records_path = Path(self.tmp.name) / "records.jsonl"
        self.patch = patch.object(atlas, "RECORDS", self.records_path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _call(self, args: dict) -> dict:
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "atlas_suggest", "arguments": args}}
        return mcp_server.handle(req)

    def test_mcp_suggest_empty_store_returns_clean_text(self):
        resp = self._call({})
        self.assertFalse(resp["result"]["isError"])
        text = resp["result"]["content"][0]["text"]
        self.assertIn("No suggestions", text)

    def test_mcp_suggest_with_stale_goal(self):
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        self.records_path.write_text(json.dumps({
            "id": "goal-1", "ts": _ts(atlas._GOAL_STALE_DAYS + 10), "kind": "goal",
            "entity": "project:api", "status": "active",
            "text": "ship stable API",
            "source": {"kind": "test", "ref": "test"},
            "confidence": 1.0, "tags": [],
            "supersedes": None, "relations": [],
        }) + "\n", encoding="utf-8")
        resp = self._call({})
        self.assertFalse(resp["result"]["isError"])
        text = resp["result"]["content"][0]["text"]
        self.assertIn("project:api", text)
        self.assertIn("review-goal", text)

    def test_mcp_suggest_entity_filter(self):
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"id": "ga", "ts": _ts(atlas._GOAL_STALE_DAYS + 5), "kind": "goal",
             "entity": "project:a", "status": "active", "text": "goal a",
             "source": {"kind": "test", "ref": "t"}, "confidence": 1.0,
             "tags": [], "supersedes": None, "relations": []},
            {"id": "gb", "ts": _ts(atlas._GOAL_STALE_DAYS + 5), "kind": "goal",
             "entity": "project:b", "status": "active", "text": "goal b",
             "source": {"kind": "test", "ref": "t"}, "confidence": 1.0,
             "tags": [], "supersedes": None, "relations": []},
        ]
        self.records_path.write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
        )
        resp = self._call({"entity": "project:a"})
        self.assertFalse(resp["result"]["isError"])
        text = resp["result"]["content"][0]["text"]
        self.assertIn("project:a", text)
        self.assertNotIn("project:b", text)

    def test_mcp_suggest_in_tools_list(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = mcp_server.handle(req)
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertIn("atlas_suggest", names)

    def test_mcp_suggest_tool_has_description(self):
        req = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        resp = mcp_server.handle(req)
        tool = next(t for t in resp["result"]["tools"] if t["name"] == "atlas_suggest")
        self.assertIn("suggestion", tool["description"].lower())


if __name__ == "__main__":
    unittest.main()
