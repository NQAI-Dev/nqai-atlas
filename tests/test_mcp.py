"""End-to-end MCP stdio tests run against an isolated temp records.jsonl."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class McpClient:
    def __init__(self, records: Path):
        env = {**os.environ, "ATLAS_RECORDS": str(records)}
        self.proc = subprocess.Popen(
            [sys.executable, str(REPO / "mcp_server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        self._id = 0

    def call(self, method: str, params: dict | None = None):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        for line in self.proc.stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") == self._id:
                return response
        raise AssertionError("no response for " + method)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records = Path(self.tmp.name) / "records.jsonl"
        self.client = McpClient(self.records)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_initialize_handshake(self):
        response = self.client.call("initialize")
        result = response["result"]
        self.assertEqual(result["serverInfo"]["name"], "nqai-atlas")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("resources", result["capabilities"])

    def test_tools_list_contains_expected(self):
        response = self.client.call("tools/list")
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            names,
            {"atlas_search", "atlas_add", "atlas_context", "atlas_health_check",
             "atlas_observe_projects", "atlas_explain", "atlas_verify"},
        )

    def test_resources_list_and_read_empty(self):
        list_resp = self.client.call("resources/list")
        self.assertEqual({r["uri"] for r in list_resp["result"]["resources"]}, {"atlas://records"})

        read_resp = self.client.call("resources/read", {"uri": "atlas://records"})
        contents = read_resp["result"]["contents"]
        self.assertEqual(contents[0]["mimeType"], "application/jsonl")
        self.assertEqual(contents[0]["text"], "")

    def test_atlas_add_and_search_round_trip(self):
        add_resp = self.client.call("tools/call", {
            "name": "atlas_add",
            "arguments": {"kind": "fact", "entity": "project:mcp", "text": "hello", "source": "test"},
        })
        self.assertFalse(add_resp["result"]["isError"])
        self.assertIn("rec_", add_resp["result"]["content"][0]["text"])

        search_resp = self.client.call("tools/call", {
            "name": "atlas_search",
            "arguments": {"entity": "project:mcp", "active": True},
        })
        self.assertFalse(search_resp["result"]["isError"])
        self.assertIn("hello", search_resp["result"]["content"][0]["text"])

    def test_atlas_health_check_round_trip(self):
        response = self.client.call("tools/call", {
            "name": "atlas_health_check",
            "arguments": {
                "entity": "service:mcp", "status": "healthy", "source": "probe:mcp",
                "checked_at": "2026-09-18T20:00:00Z",
            },
        })
        self.assertFalse(response["result"]["isError"])
        self.assertIn("health-check record", response["result"]["content"][0]["text"])

        search = self.client.call("tools/call", {
            "name": "atlas_search", "arguments": {"entity": "service:mcp", "active": True},
        })
        self.assertIn("2026-09-18T20:00:00Z", search["result"]["content"][0]["text"])

    def test_atlas_add_rejects_missing_required(self):
        response = self.client.call("tools/call", {
            "name": "atlas_add",
            "arguments": {"kind": "fact", "entity": "project:mcp"},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("missing required fields", response["result"]["content"][0]["text"])

    def test_atlas_add_rejects_unknown_tool(self):
        response = self.client.call("tools/call", {"name": "atlas_nope", "arguments": {}})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("unknown tool", response["result"]["content"][0]["text"])

    def test_atlas_explain_unknown_entity(self):
        response = self.client.call("tools/call", {
            "name": "atlas_explain", "arguments": {"entity": "project:none"},
        })
        self.assertIn("No records", response["result"]["content"][0]["text"])

    def test_atlas_verify_clean_passes(self):
        self.client.call("tools/call", {
            "name": "atlas_add",
            "arguments": {"kind": "fact", "entity": "project:mcp", "text": "ok", "source": "test"},
        })
        response = self.client.call("tools/call", {"name": "atlas_verify", "arguments": {}})
        self.assertFalse(response["result"]["isError"])
        self.assertIn("passed", response["result"]["content"][0]["text"])

    def test_ping_returns_empty_result(self):
        response = self.client.call("ping")
        self.assertEqual(response["result"], {})


if __name__ == "__main__":
    unittest.main()
