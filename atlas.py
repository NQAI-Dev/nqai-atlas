#!/usr/bin/env python3
"""NQAI Atlas: a tiny local operational registry for projects and services."""
from __future__ import annotations
import argparse, json, subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "atlas.json"
DEFAULT_SCAN = Path("/home/openclaw/Projects")

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def git(path, *args):
    p = subprocess.run(["git", "-C", str(path), *args], text=True, capture_output=True)
    return p.stdout.strip() if p.returncode == 0 else None

def scan(root: Path):
    projects = []
    for gitdir in sorted(root.glob("*/.git")):
        path = gitdir.parent
        if path.resolve() == ROOT.resolve():
            continue
        branch = git(path, "branch", "--show-current") or "detached"
        status = git(path, "status", "--porcelain") or ""
        projects.append({"name": path.name, "path": str(path), "branch": branch,
                         "dirty": bool(status), "changed": len(status.splitlines()),
                         "last_commit": git(path, "log", "-1", "--format=%h %s")})
    return projects

def load():
    if not REGISTRY.exists(): return {"version": 1, "updated_at": None, "projects": []}
    return json.loads(REGISTRY.read_text())
def save(data):
    REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
def cmd_scan(args):
    data = load(); data["projects"] = scan(Path(args.root)); data["updated_at"] = now(); save(data)
    print(f"Indexed {len(data['projects'])} Git projects at {data['updated_at']}")
def cmd_status(_args):
    data = load()
    if not data["projects"]: print("Atlas is empty. Run: ./atlas.py scan"); return 1
    print(f"Atlas updated {data['updated_at']}")
    for p in data["projects"]:
        mark = "DIRTY" if p["dirty"] else "clean"
        print(f"{mark:5} {p['name']:24} {p['branch']:12} {p['changed']:>3} changes  {p['last_commit'] or '-'}")
def cmd_check(_args):
    data = load(); dirty = [p for p in data["projects"] if p["dirty"]]
    print(f"projects={len(data['projects'])} dirty={len(dirty)}")
    return 1 if dirty else 0

def main():
    ap = argparse.ArgumentParser(description=__doc__); sub = ap.add_subparsers(required=True)
    s = sub.add_parser("scan"); s.add_argument("--root", default=str(DEFAULT_SCAN)); s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("check"); s.set_defaults(fn=cmd_check)
    args = ap.parse_args(); raise SystemExit(args.fn(args) or 0)
if __name__ == "__main__": main()
