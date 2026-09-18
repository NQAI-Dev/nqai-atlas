#!/usr/bin/env python3
"""NQAI Atlas: append-only storage for durable facts and decisions."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RECORDS = Path(os.environ.get("ATLAS_RECORDS") or (ROOT / "data" / "records.jsonl"))
DEFAULT_PROJECTS = Path("/home/openclaw/Projects")
KINDS = {"fact", "decision", "goal", "observation", "link"}
STATUSES = {"active", "superseded", "archived"}
ENTITY_TYPES = {"nqai", "project", "service", "host", "person", "decision", "goal", "unknown"}
RELATION_TYPES = {"relates_to", "depends_on", "runs_on", "owns", "tracks"}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def records():
    if not RECORDS.exists():
        return []
    with RECORDS.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    existing = {record["id"] for record in records()}
    candidate = f"rec_{stamp}"
    index = 1
    while candidate in existing:
        index += 1
        candidate = f"rec_{stamp}_{index:02d}"
    return candidate


def git_output(path: Path, *arguments: str) -> str | None:
    process = subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, text=True)
    return process.stdout.strip() if process.returncode == 0 else None


def active_records(entity: str | None = None) -> list[dict]:
    items = records()
    superseded = {item.get("supersedes") for item in items if item.get("supersedes")}
    return [item for item in items
            if item.get("status") == "active" and item.get("id") not in superseded
            and (entity is None or item.get("entity") == entity)]


def current_records(entity: str) -> list[dict]:
    """Return current records while preserving independent observation streams."""
    current = active_records(entity)
    non_observations = [item for item in current if item.get("kind") != "observation"]
    observations: dict[tuple, dict] = {}
    for item in current:
        if item.get("kind") != "observation":
            continue
        source = item.get("source", {})
        stream = (
            tuple(sorted(item.get("tags", []))),
            source.get("kind"),
            source.get("ref"),
        )
        previous = observations.get(stream)
        if previous is None or item.get("ts", "") > previous.get("ts", ""):
            observations[stream] = item
    return non_observations + list(observations.values())


def entity_type(entity: str) -> str:
    return entity.split(":", 1)[0] if ":" in entity else ("nqai" if entity == "nqai-atlas" else "unknown")


def validate_record(record: dict, seen: set[str] | None = None) -> list[str]:
    errors = []
    required = {"id", "ts", "kind", "entity", "text", "status", "source", "confidence", "tags", "supersedes"}
    missing = required - record.keys()
    if missing:
        errors.append(f"missing {', '.join(sorted(missing))}")
    if entity_type(record.get("entity", "")) not in ENTITY_TYPES:
        errors.append(f"invalid entity type: {record.get('entity')}")
    if not isinstance(record.get("relations", []), list):
        errors.append("relations must be a list")
    for relation in record.get("relations", []):
        if not isinstance(relation, dict) or relation.get("type") not in RELATION_TYPES or not relation.get("entity"):
            errors.append(f"invalid relation: {relation}")
    if seen is not None and record.get("supersedes") and record["supersedes"] not in seen:
        errors.append(f"unknown supersedes {record['supersedes']}")
    return errors


def append_record(record: dict) -> None:
    RECORDS.parent.mkdir(parents=True, exist_ok=True)
    with RECORDS.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def project_inventory(root: Path) -> list[dict]:
    result = []
    for git_dir in sorted(root.glob("*/.git")):
        path = git_dir.parent
        branch = git_output(path, "branch", "--show-current") or "detached"
        status = git_output(path, "status", "--porcelain") or ""
        result.append({
            "name": path.name, "path": str(path), "branch": branch,
            "dirty": bool(status), "changed": len(status.splitlines()),
            "last_commit": git_output(path, "log", "-1", "--format=%h %s"),
        })
    return result


def record_project_observations(root: Path) -> tuple[int, int]:
    """Append changed Git snapshots and preserve unrelated entity records."""
    appended = 0
    skipped = 0
    for project in project_inventory(root):
        entity = f"project:{project['name']}"
        text = json.dumps(project, ensure_ascii=False, sort_keys=True)
        previous = next((
            item for item in reversed(active_records(entity))
            if item.get("kind") == "observation" and "git" in item.get("tags", [])
        ), None)
        if previous and previous.get("text") == text:
            skipped += 1
            continue
        record = {
            "id": new_id(), "ts": timestamp(), "kind": "observation",
            "entity": entity, "text": text, "status": "active",
            "source": {"kind": "filesystem", "ref": project["path"]},
            "confidence": 1.0, "tags": ["git", "inventory"],
            "supersedes": previous["id"] if previous else None,
            "relations": [{"type": "relates_to", "entity": "nqai-atlas"}],
        }
        append_record(record)
        appended += 1
    return appended, skipped


def record_health_check(entity: str, status: str, source: str, checked_at: str | None = None,
                        detail: str | None = None, source_kind: str = "health-check") -> dict:
    """Append a health observation and supersede the prior result from this source."""
    if status not in {"healthy", "degraded", "unhealthy", "unknown"}:
        raise ValueError("health status must be healthy, degraded, unhealthy, or unknown")
    observed_at = checked_at or timestamp()
    try:
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("checked_at must be an ISO-8601 timestamp") from error
    previous = next((
        item for item in reversed(active_records(entity))
        if item.get("kind") == "observation"
        and "health-check" in item.get("tags", [])
        and item.get("source", {}).get("kind") == source_kind
        and item.get("source", {}).get("ref") == source
    ), None)
    payload = {"status": status, "checked_at": observed_at}
    if detail:
        payload["detail"] = detail
    record = {
        "id": new_id(), "ts": timestamp(), "kind": "observation",
        "entity": entity, "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "status": "active", "source": {"kind": source_kind, "ref": source},
        "confidence": 1.0, "tags": ["health-check", status],
        "supersedes": previous["id"] if previous else None, "relations": [],
    }
    append_record(record)
    return record


def health_check(args) -> int:
    record = record_health_check(
        args.entity, args.health_status, args.source, args.checked_at, args.detail,
        getattr(args, "source_kind", "health-check"),
    )
    print(record["id"])
    return 0

def observe_projects(args) -> int:
    appended, skipped = record_project_observations(Path(args.root))
    print(f"observed projects: {appended}; unchanged: {skipped}")
    return 0


def add(args) -> int:
    if args.kind not in KINDS:
        raise SystemExit(f"invalid kind: {args.kind}")
    record = {
        "id": new_id(), "ts": timestamp(), "kind": args.kind,
        "entity": args.entity, "text": args.text, "status": args.status,
        "source": {"kind": "conversation", "ref": args.source},
        "confidence": args.confidence, "tags": args.tag, "supersedes": args.supersedes,
        "relations": getattr(args, "relation", []),
    }
    RECORDS.parent.mkdir(parents=True, exist_ok=True)
    with RECORDS.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(record["id"])
    return 0


def search(args) -> int:
    found = [r for r in records() if (not args.entity or r["entity"] == args.entity)
             and (not args.kind or r["kind"] == args.kind)
             and (not args.active or r["status"] == "active")
             and (not args.text or args.text.lower() in r["text"].lower())]
    for record in found:
        print(f"{record['id']} [{record['kind']}] {record['entity']}: {record['text']}")
    return 0


def context_entity(entity: str) -> str:
    items = records()
    own = current_records(entity)
    related = [record for record in items if any(link.get("entity") == entity for link in record.get("relations", []))]
    related_entities = {record.get("entity") for record in related}
    related = [record for related_entity in related_entities for record in current_records(related_entity)]
    if not own and not related:
        return f"No context for entity: {entity}"
    lines = [f"Context: {entity}", f"Own records: {len(own)}", f"Related records: {len(related)}"]
    for record in own + [item for item in related if item not in own]:
        marker = "OWN" if record in own else "RELATED"
        lines.append(f"{marker} {record['id']} [{record['kind']}] {record['status']}: {record['text']}")
    return "\n".join(lines)


def history_entity(entity: str) -> str:
    """Show every record for an entity in chronological order without hiding old claims."""
    items = [record for record in records() if record["entity"] == entity]
    if not items:
        return f"No history for entity: {entity}"
    superseded_by = {
        record["supersedes"]: record["id"]
        for record in items
        if record.get("supersedes")
    }
    lines = [f"History: {entity}", f"Records: {len(items)}"]
    for record in sorted(items, key=lambda item: (item.get("ts", ""), item["id"])):
        details = [record["status"]]
        if record.get("supersedes"):
            details.append(f"supersedes={record['supersedes']}")
        if record["id"] in superseded_by:
            details.append(f"superseded_by={superseded_by[record['id']]}")
        lines.append(
            f"{record['ts']} {record['id']} [{record['kind']}] "
            f"{' '.join(details)}: {record['text']}"
        )
    return "\n".join(lines)


def explain_entity(entity: str) -> str:
    items = [record for record in records() if record["entity"] == entity]
    if not items:
        return f"No records for entity: {entity}"
    active = current_records(entity)
    lines = [f"Entity: {entity}", f"Active records: {len(active)}"]
    for record in active:
        lines.append(f"CURRENT {record['id']} [{record['kind']}] {record['text']}")
        previous = record.get("supersedes")
        while previous:
            old = next((item for item in items if item["id"] == previous), None)
            if old is None:
                lines.append(f"  BROKEN supersedes: {previous}")
                break
            lines.append(f"  <- {old['id']} [{old['status']}] {old['text']}")
            previous = old.get("supersedes")
    return "\n".join(lines)


def explain(args) -> int:
    print(explain_entity(args.entity))
    return 0


def verify(_args) -> int:
    seen = set()
    errors = []
    for line_no, line in enumerate(RECORDS.read_text(encoding="utf-8").splitlines(), 1) if RECORDS.exists() else []:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(f"line {line_no}: invalid JSON ({error.msg})")
            continue
        errors.extend(f"line {line_no}: {error}" for error in validate_record(record, seen))
        if record.get("id") in seen: errors.append(f"line {line_no}: duplicate id {record.get('id')}")
        seen.add(record.get("id"))
        if record.get("kind") not in KINDS: errors.append(f"line {line_no}: invalid kind")
        if record.get("status") not in STATUSES: errors.append(f"line {line_no}: invalid status")
        try: datetime.fromisoformat(record.get("ts", "").replace("Z", "+00:00"))
        except ValueError: errors.append(f"line {line_no}: invalid timestamp")
    for record in records():
        if record.get("supersedes") and record["supersedes"] not in seen:
            errors.append(f"{record['id']}: unknown supersedes {record['supersedes']}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"valid records: {len(seen)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)
    command = sub.add_parser("add"); command.add_argument("--kind", required=True); command.add_argument("--entity", required=True); command.add_argument("--text", required=True); command.add_argument("--source", required=True); command.add_argument("--status", default="active", choices=sorted(STATUSES)); command.add_argument("--confidence", type=float, default=1.0); command.add_argument("--tag", action="append", default=[]); command.add_argument("--supersedes"); command.add_argument("--relation", action="append", default=[], type=lambda value: {"type": "relates_to", "entity": value}); command.set_defaults(fn=add)
    command = sub.add_parser("health-check"); command.add_argument("--entity", required=True); command.add_argument("--status", dest="health_status", required=True, choices=["healthy", "degraded", "unhealthy", "unknown"]); command.add_argument("--source", required=True); command.add_argument("--source-kind", default="health-check"); command.add_argument("--checked-at"); command.add_argument("--detail"); command.set_defaults(fn=health_check)
    command = sub.add_parser("search"); command.add_argument("--entity"); command.add_argument("--kind"); command.add_argument("--text"); command.add_argument("--active", action="store_true"); command.set_defaults(fn=search)
    command = sub.add_parser("context"); command.add_argument("--entity", required=True); command.set_defaults(fn=lambda args: print(context_entity(args.entity)) or 0)
    command = sub.add_parser("history"); command.add_argument("--entity", required=True); command.set_defaults(fn=lambda args: print(history_entity(args.entity)) or 0)
    command = sub.add_parser("observe-projects"); command.add_argument("--root", default=str(DEFAULT_PROJECTS)); command.set_defaults(fn=observe_projects)
    command = sub.add_parser("explain"); command.add_argument("--entity", required=True); command.set_defaults(fn=explain)
    command = sub.add_parser("verify"); command.set_defaults(fn=verify)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
