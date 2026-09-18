#!/usr/bin/env python3
"""Minimal stdio MCP server for NQAI Atlas; stdlib-only."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import atlas  # noqa: E402

SERVER_INFO = {"name": "nqai-atlas", "version": "0.1.0"}
PROTOCOL = "2025-06-18"


def result(request_id, value):
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def records_text(items):
    if not items:
        return "No matching Atlas records."
    return "\n".join(
        f"{r['id']} [{r['kind']}] {r['entity']} ({r['status']}): {r['text']}"
        for r in items
    )


def call_tool(name, args):
    if name == "atlas_search":
        matches = [
            r for r in atlas.active_records(args.get("entity"))
            if (not args.get("entity") or r["entity"] == args["entity"])
            and (not args.get("kind") or r["kind"] == args["kind"])
            and (not args.get("active") or r["status"] == "active")
            and (not args.get("text") or args["text"].lower() in r["text"].lower())
        ]
        return records_text(matches)
    if name == "atlas_add":
        kind = args.get("kind")
        if kind not in atlas.KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(atlas.KINDS))}")
        required = ("entity", "text", "source")
        missing = [key for key in required if not args.get(key)]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")
        record = {
            "id": atlas.new_id(), "ts": atlas.timestamp(), "kind": kind,
            "entity": args["entity"], "text": args["text"],
            "status": args.get("status", "active"),
            "source": {"kind": args.get("source_kind", "conversation"), "ref": args["source"]},
            "confidence": float(args.get("confidence", 1.0)),
            "tags": args.get("tags", []), "supersedes": args.get("supersedes"),
            "relations": [{"type": "relates_to", "entity": related_entity} for related_entity in args.get("relations", [])],
        }
        if record["status"] not in atlas.STATUSES:
            raise ValueError("invalid status")
        atlas.RECORDS.parent.mkdir(parents=True, exist_ok=True)
        with atlas.RECORDS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return f"Added Atlas record {record['id']}."
    if name == "atlas_health_check":
        required = ("entity", "status", "source")
        missing = [key for key in required if not args.get(key)]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")
        record = atlas.record_health_check(
            args["entity"], args["status"], args["source"], args.get("checked_at"),
            args.get("detail"), args.get("source_kind", "health-check"),
        )
        return f"Added health-check record {record['id']}."
    if name == "atlas_observe_projects":
        root = Path(args.get("root", str(atlas.DEFAULT_PROJECTS)))
        appended, skipped = atlas.record_project_observations(root)
        return f"Observed projects: {appended}; unchanged: {skipped}."
    if name == "atlas_context":
        entity = args.get("entity")
        if not entity:
            raise ValueError("entity is required")
        return atlas.context_entity(entity)
    if name == "atlas_explain":
        entity = args.get("entity")
        if not entity:
            raise ValueError("entity is required")
        return atlas.explain_entity(entity)
    if name == "atlas_verify":
        code = atlas.verify(type("Args", (), {})())
        if code:
            raise ValueError("Atlas verification failed; inspect stderr or run `python3 atlas.py verify`.")
        return "Atlas verification passed."
    raise ValueError(f"unknown tool: {name}")


def handle(req):
    method, request_id = req.get("method"), req.get("id")
    if method == "initialize":
        return result(request_id, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}, "resources": {}}, "serverInfo": SERVER_INFO})
    if method == "ping":
        return result(request_id, {})
    if method == "tools/list":
        return result(request_id, {"tools": [
            {"name": "atlas_search", "description": "Search durable Atlas records; inactive records are excluded only when active=true.", "inputSchema": {"type": "object", "properties": {"entity": {"type": "string"}, "kind": {"type": "string", "enum": sorted(atlas.KINDS)}, "text": {"type": "string"}, "active": {"type": "boolean"}}}},
            {"name": "atlas_add", "description": "Append a durable fact, decision, goal, observation, or link to Atlas.", "inputSchema": {"type": "object", "required": ["kind", "entity", "text", "source"], "properties": {"kind": {"type": "string", "enum": sorted(atlas.KINDS)}, "entity": {"type": "string"}, "text": {"type": "string"}, "source": {"type": "string"}, "source_kind": {"type": "string"}, "status": {"type": "string", "enum": sorted(atlas.STATUSES)}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "tags": {"type": "array", "items": {"type": "string"}}, "supersedes": {"type": ["string", "null"]}, "relations": {"type": "array", "items": {"type": "string"}}}}},
            {"name": "atlas_context", "description": "Show an entity's own records and records explicitly related to it.", "inputSchema": {"type": "object", "required": ["entity"], "properties": {"entity": {"type": "string"}}}},
            {"name": "atlas_health_check", "description": "Append a timestamped health observation and supersede the prior result from the same source.", "inputSchema": {"type": "object", "required": ["entity", "status", "source"], "properties": {"entity": {"type": "string"}, "status": {"type": "string", "enum": ["healthy", "degraded", "unhealthy", "unknown"]}, "source": {"type": "string"}, "source_kind": {"type": "string"}, "checked_at": {"type": "string"}, "detail": {"type": "string"}}}},
            {"name": "atlas_observe_projects", "description": "Append a read-only Git inventory observation for local projects.", "inputSchema": {"type": "object", "properties": {"root": {"type": "string"}}}},
            {"name": "atlas_explain", "description": "Explain the current active records for an entity and show their supersedes history.", "inputSchema": {"type": "object", "required": ["entity"], "properties": {"entity": {"type": "string"}}}},
            {"name": "atlas_verify", "description": "Validate Atlas JSONL integrity, IDs, timestamps, kinds, statuses, and supersedes links.", "inputSchema": {"type": "object", "properties": {}}},
        ]})
    if method == "resources/list":
        return result(request_id, {"resources": [{"uri": "atlas://records", "name": "Atlas records", "description": "Current durable Atlas records", "mimeType": "application/jsonl"}]})
    if method == "resources/read":
        if req.get("params", {}).get("uri") != "atlas://records":
            return error(request_id, -32602, "unknown resource")
        text = atlas.RECORDS.read_text(encoding="utf-8") if atlas.RECORDS.exists() else ""
        return result(request_id, {"contents": [{"uri": "atlas://records", "mimeType": "application/jsonl", "text": text}]})
    if method == "tools/call":
        params = req.get("params", {})
        try:
            text = call_tool(params.get("name", ""), params.get("arguments", {}))
            return result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except (ValueError, KeyError, TypeError) as exc:
            return result(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
    if request_id is None:
        return None
    return error(request_id, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        try:
            req = json.loads(line)
            response = handle(req)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
        except json.JSONDecodeError as exc:
            print(json.dumps(error(None, -32700, str(exc))), flush=True)


if __name__ == "__main__":
    main()
