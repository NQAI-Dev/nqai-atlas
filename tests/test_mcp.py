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
             "atlas_observe_projects", "atlas_explain", "atlas_history", "atlas_verify", "atlas_suggest", "atlas_review", "atlas_progress"},
        )

    def test_resources_list_and_read_empty(self):
        list_resp = self.client.call("resources/list")
        self.assertEqual({r["uri"] for r in list_resp["result"]["resources"]}, {"atlas://records"})

        templates_resp = self.client.call("resources/templates/list")
        templates = templates_resp["result"]["resourceTemplates"]
        self.assertEqual(templates[0]["uriTemplate"], "atlas://entity/{entity}")

        read_resp = self.client.call("resources/read", {"uri": "atlas://records"})
        contents = read_resp["result"]["contents"]
        self.assertEqual(contents[0]["mimeType"], "application/jsonl")
        self.assertEqual(contents[0]["text"], "")

    def test_entity_context_resource_template_and_read(self):
        templates_resp = self.client.call("resources/templates/list")
        templates = templates_resp["result"]["resourceTemplates"]
        self.assertEqual(templates[0]["uriTemplate"], "atlas://entity/{entity}")
        self.assertEqual(templates[0]["mimeType"], "text/plain")

        add_resp = self.client.call("tools/call", {
            "name": "atlas_add",
            "arguments": {
                "kind": "fact", "entity": "project:mcp/demo", "text": "resource context",
                "source": "test",
            },
        })
        self.assertFalse(add_resp["result"]["isError"])

        uri = "atlas://entity/project%3Amcp%2Fdemo"
        read_resp = self.client.call("resources/read", {"uri": uri})
        contents = read_resp["result"]["contents"]
        self.assertEqual(contents[0]["uri"], uri)
        self.assertEqual(contents[0]["mimeType"], "text/plain")
        self.assertIn("Context: project:mcp/demo", contents[0]["text"])
        self.assertIn("resource context", contents[0]["text"])

    def test_entity_context_resource_rejects_malformed_uri(self):
        response = self.client.call("resources/read", {"uri": "atlas://entity/"})
        self.assertEqual(response["error"]["code"], -32602)

    def test_entity_context_resource(self):
        self.client.call("tools/call", {
            "name": "atlas_add",
            "arguments": {
                "kind": "fact", "entity": "project:mcp", "text": "resource context",
                "source": "test", "relations": ["service:mcp"],
            },
        })

        response = self.client.call("resources/read", {"uri": "atlas://entity/project%3Amcp"})
        contents = response["result"]["contents"]
        self.assertEqual(contents[0]["mimeType"], "text/plain")
        self.assertIn("Context: project:mcp", contents[0]["text"])
        self.assertIn("resource context", contents[0]["text"])

    def test_entity_context_resource_unknown_entity(self):
        response = self.client.call("resources/read", {"uri": "atlas://entity/project%3Anone"})
        self.assertEqual(
            response["result"]["contents"][0]["text"],
            "No context for entity: project:none",
        )

    def test_entity_context_resource_rejects_empty_entity(self):
        response = self.client.call("resources/read", {"uri": "atlas://entity/"})
        self.assertEqual(response["error"]["code"], -32602)

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

    def test_atlas_history_returns_all_entity_records(self):
        first = self.client.call("tools/call", {
            "name": "atlas_add",
            "arguments": {"kind": "fact", "entity": "project:mcp", "text": "first", "source": "test"},
        })
        first_id = first["result"]["content"][0]["text"].split()[-1].rstrip(".")
        self.client.call("tools/call", {
            "name": "atlas_add",
            "arguments": {"kind": "fact", "entity": "project:mcp", "text": "second", "source": "test", "supersedes": first_id},
        })

        response = self.client.call("tools/call", {
            "name": "atlas_history", "arguments": {"entity": "project:mcp"},
        })
        self.assertFalse(response["result"]["isError"])
        text = response["result"]["content"][0]["text"]
        self.assertIn("Records: 2", text)
        self.assertIn("superseded_by=", text)
        self.assertIn("supersedes=", text)

    def test_ping_returns_empty_result(self):
        response = self.client.call("ping")
        self.assertEqual(response["result"], {})


if __name__ == "__main__":
    unittest.main()
