"""Tests for review_report/render_review and the atlas_review MCP tool."""
from __future__ import annotations

import json
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


class ReviewTests(unittest.TestCase):
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

    def test_empty_store_review(self):
        report = atlas.review_report()
        self.assertEqual(report["total_records"], 0)
        self.assertEqual(report["recent_records"], 0)
        self.assertEqual(report["open_goals"], 0)
        self.assertEqual(report["suggestions"], [])
        self.assertEqual(report["window_days"], 7)
        rendered = atlas.render_review(report)
        self.assertIn("No stale items", rendered)

    def test_counts_recent_activity_any_status(self):
        self._write([
            _record("project:alpha", "fact", "recent fact", ts=_ts(1)),
            _record("project:alpha", "fact", "old fact", ts=_ts(30)),
            _record("project:beta", "decision", "superseded recent",
                    status="superseded", ts=_ts(2)),
        ])
        report = atlas.review_report()
        self.assertEqual(report["total_records"], 3)
        self.assertEqual(report["recent_records"], 2)
        self.assertEqual(report["open_goals"], 0)

    def test_custom_window_days(self):
        self._write([
            _record("project:alpha", "fact", "ten days old", ts=_ts(10)),
        ])
        report = atlas.review_report(days=14)
        self.assertEqual(report["recent_records"], 1)
        self.assertEqual(report["window_days"], 14)
        self.assertEqual(report["recent_records"], 1)

    def test_open_goals_counts_current_goals_only(self):
        self._write([
            _record("project:alpha", "goal", "open goal", ts=_ts(1)),
            _record("project:alpha", "goal", "old goal", status="archived", ts=_ts(2)),
        ])
        report = atlas.review_report()
        self.assertEqual(report["open_goals"], 1)

    def test_entity_filter_scopes_report(self):
        self._write([
            _record("project:alpha", "fact", "alpha fact", ts=_ts(0)),
            _record("project:beta", "fact", "beta fact", ts=_ts(0)),
        ])
        report = atlas.review_report(entity="project:alpha")
        self.assertEqual(report["entity"], "project:alpha")
        self.assertEqual(report["total_records"], 1)

    def test_suggestions_grouped_by_priority_in_render(self):
        self._write([
            _record("project:alpha", "goal", "ancient goal", ts=_ts(60)),   # high
            _record("project:beta", "decision", "old decision", ts=_ts(40)),  # medium
            _record("project:gamma", "observation",
                    json.dumps({"name": "gamma", "dirty": False, "changed": 0}),
                    tags=["git", "inventory"], ts=_ts(0)),
        ])
        report = atlas.review_report()
        priorities = [s["priority"] for s in report["suggestions"]]
        self.assertIn("high", priorities)
        self.assertIn("medium", priorities)
        rendered = atlas.render_review(report)
        high_index = rendered.index("HIGH")
        medium_index = rendered.index("MEDIUM")
        self.assertLess(high_index, medium_index)
        self.assertIn("rec_project_alpha_goal", rendered)

    def test_invalid_days_rejected(self):
        with self.assertRaises(ValueError):
            atlas.review_report(days=0)


class ReviewMcpTests(unittest.TestCase):
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

    def test_atlas_review_returns_digest(self):
        self._write([
            _record("project:alpha", "goal", "ancient goal", ts=_ts(60)),
            _record("project:alpha", "fact", "fresh fact", ts=_ts(1)),
        ])
        text = mcp_server.call_tool("atlas_review", {})
        self.assertIn("Atlas review", text)
        self.assertIn("open goals: 1", text)
        self.assertIn("review-goal", text)

    def test_atlas_review_entity_and_days_filters(self):
        self._write([
            _record("project:alpha", "goal", "ancient goal", ts=_ts(60)),
            _record("project:beta", "fact", "fresh beta fact", ts=_ts(1)),
        ])
        text = mcp_server.call_tool("atlas_review", {"entity": "project:beta"})
        self.assertIn("for project:beta", text)
        self.assertNotIn("review-goal", text)
        text_all = mcp_server.call_tool("atlas_review", {"days": 2})
        self.assertIn("recent: 1", text_all)

    def test_atlas_review_clean_store(self):
        self._write([])
        text = mcp_server.call_tool("atlas_review", {})
        self.assertIn("No stale items", text)

    def test_atlas_review_invalid_days_is_error(self):
        result = mcp_server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "atlas_review", "arguments": {"days": 0}},
        })
        self.assertTrue(result["result"]["isError"])

    def test_tools_list_advertises_atlas_review(self):
        result = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in result["result"]["tools"]]
        self.assertIn("atlas_review", names)


if __name__ == "__main__":
    unittest.main()
