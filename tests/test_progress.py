"""Tests for progress_report/render_progress and the atlas_progress MCP tool."""
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

def _record(entity: str, kind: str, text: str, tags: list | None = None, ts: str | None = None) -> dict:
    return {
        "id": f"rec_{entity}_{kind}",
        "ts": ts or _ts(0),
        "kind": kind,
        "entity": entity,
        "text": text,
        "status": "active",
        "source": {"kind": "test", "ref": "test"},
        "confidence": 1.0,
        "tags": tags or [],
        "supersedes": None,
        "relations": [],
    }

class ProgressTests(unittest.TestCase):
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
        self.records_path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def test_progress_report_identifies_evidence(self):
        self._write([
            _record("project:alpha", "fact", "we have a new commit", ts=_ts(1)),
            _record("project:alpha", "fact", "wrote a test suite", ts=_ts(1)),
            _record("project:alpha", "fact", "hit a blocker on CI", tags=["blocker:ci"], ts=_ts(1)),
            _record("project:alpha", "fact", "read a URL http://test", ts=_ts(1)),
            _record("project:alpha", "fact", "plain text nothing to see", ts=_ts(1)),
            _record("project:alpha", "decision", "we decided this", ts=_ts(1)),
            _record("project:alpha", "goal", "a goal without evidence", ts=_ts(1))
        ])
        report = atlas.progress_report()
        self.assertEqual(len(report["evidence"]), 6)
        ev_types = set()
        for ev in report["evidence"]:
            ev_types.update(ev["evidence_types"])
        self.assertIn("commit", ev_types)
        self.assertIn("test", ev_types)
        self.assertIn("blocker", ev_types)
        self.assertIn("url", ev_types)

    def test_atlas_progress_tool(self):
        self._write([
            _record("project:alpha", "fact", "we have a new commit", ts=_ts(1))
        ])
        result = mcp_server.call_tool("atlas_progress", {})
        self.assertIn("Atlas progress report", result)
        self.assertIn("[commit]", result)
        self.assertIn("we have a new commit", result)

    def test_tools_list_advertises_atlas_progress(self):
        result = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in result["result"]["tools"]}
        self.assertIn("atlas_progress", names)

if __name__ == "__main__":
    unittest.main()
