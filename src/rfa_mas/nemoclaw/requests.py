"""Blocked network requests: collect OpenShell DENIED decisions, approve or deny them from the CLI.

``sync`` reads ``nemoclaw <sb> logs`` (OCSF lines) and records each distinct denied destination.
``approve`` turns one into a reviewed custom preset (``sg-approved-<id>``) applied with
``nemoclaw <sb> policy add --from-file``; ``deny`` records the decision. Both are audited so the
screen shows the approval history next to the policy blocks. ``openshell term`` remains the
interactive alternative.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw import bootstrap as bs
from rfa_mas.nemoclaw.config import load_assignments
from rfa_mas.nemoclaw.runner import Runner, SubprocessRunner, strip_warnings

DENIED = re.compile(
    r"DENIED\s+(?P<binary>/\S+?)\((?P<pid>\d+)\)\s+->\s+(?:(?P<method>[A-Z]+)\s+)?(?P<target>\S+)(?:\s+\[(?P<meta>[^\]]*)\])?"
)
PRIVATE = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(audit.db_path()), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, sandbox TEXT, binary TEXT, method TEXT,"
        " host TEXT, port INTEGER, path TEXT, reason TEXT, status TEXT, first_seen REAL, last_seen REAL, hits INTEGER)"
    )
    return conn


def parse_denied(line: str, sandbox: str) -> dict | None:
    m = DENIED.search(line)
    if not m:
        return None
    target = m.group("target")
    method = m.group("method")
    if "://" in target:
        parts = urlsplit(target)
        host, port, path = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80), parts.path or "/"
    else:
        host, _, port_text = target.rpartition(":")
        if not host:
            host, port_text = target, "443"
        try:
            port = int(port_text)
        except ValueError:
            port = 443
        path = ""
    reason = ""
    for item in (m.group("meta") or "").split():
        if item.startswith("reason:"):
            reason = item.split(":", 1)[1]
    key = f"{sandbox}|{m.group('binary')}|{host}|{port}|{method or ''}|{path}"
    return {"id": hashlib.sha1(key.encode()).hexdigest()[:10], "sandbox": sandbox, "binary": m.group("binary"),
            "method": method or "", "host": host, "port": port, "path": path, "reason": reason}


def sync(runner: Runner, sandboxes: list[str], nb: str, tail: int = 400) -> list[dict]:
    new = []
    conn = _db()
    try:
        for sandbox in sandboxes:
            result = runner.run([nb, sandbox, "logs", "--tail", str(tail)], timeout=120)
            if not result.ok:
                continue
            for line in strip_warnings(result.stdout).splitlines():
                if "DENIED" not in line:
                    continue
                item = parse_denied(line, sandbox)
                if item is None:
                    continue
                row = conn.execute("SELECT hits FROM requests WHERE id = ?", (item["id"],)).fetchone()
                now = time.time()
                if row:
                    conn.execute("UPDATE requests SET last_seen = ?, hits = hits + 1 WHERE id = ?", (now, item["id"]))
                    continue
                conn.execute(
                    "INSERT INTO requests (id, sandbox, binary, method, host, port, path, reason, status, first_seen, last_seen, hits)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                    (item["id"], sandbox, item["binary"], item["method"], item["host"], item["port"], item["path"],
                     item["reason"], "pending", now, now),
                )
                conn.commit()  # release the write lock before the audit ledger opens its own connection
                new.append(item)
                audit.record(kind="request", verdict="denied", action="policy-block", sandbox=sandbox,
                             detail={"request_id": item["id"], "host": item["host"], "port": item["port"],
                                     "method": item["method"], "path": item["path"], "binary": item["binary"],
                                     "reason": item["reason"]})
        conn.commit()
    finally:
        conn.close()
    return new


def list_requests(status: str | None = "pending") -> list[dict]:
    conn = _db()
    try:
        query = "SELECT id, sandbox, binary, method, host, port, path, reason, status, first_seen, hits FROM requests"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        rows = conn.execute(query + " ORDER BY first_seen DESC", params).fetchall()
    finally:
        conn.close()
    keys = ("id", "sandbox", "binary", "method", "host", "port", "path", "reason", "status", "first_seen", "hits")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _get(request_id: str) -> dict:
    for item in list_requests(None):
        if item["id"] == request_id:
            return item
    raise KeyError(request_id)


def approve(request_id: str, reason: str, runner: Runner, nb: str, lan_ip: str) -> dict:
    item = _get(request_id)
    name = f"sg-approved-{item['id']}"
    method = item["method"] or "GET"
    path = item["path"] or "/**"
    if not item["method"]:
        path = "/**"
    preset = {
        "preset": {"name": name, "description": f"operator-approved request {item['id']}: {reason or 'no reason given'}"},
        "network_policies": {name.replace("-", "_"): {
            "name": name.replace("-", "_"),
            "endpoints": [{"host": item["host"], "port": item["port"], "protocol": "rest", "enforcement": "enforce",
                           "rules": [{"allow": {"method": method, "path": path}}]}],
            "binaries": [{"path": item["binary"]}],
        }},
    }
    out = bs.SG_DIR / "rendered" / f"{name}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(preset, sort_keys=False), encoding="utf-8")
    argv = [nb, item["sandbox"], "policy", "add", "--from-file", str(out), "--yes"]
    if PRIVATE.match(item["host"]):
        argv += ["--trusted-private-host", item["host"]]
    result = runner.run(argv, timeout=300)
    status = "approved" if result.ok else "approve-failed"
    conn = _db()
    try:
        conn.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))
        conn.commit()
    finally:
        conn.close()
    audit.record(kind="approval", verdict=status, action="approve", sandbox=item["sandbox"],
                 detail={"request_id": request_id, "host": item["host"], "port": item["port"], "method": method,
                         "path": path, "preset": name, "reason": reason,
                         "error": None if result.ok else (result.stderr or result.stdout).strip()[-200:]})
    return {"request": item, "status": status, "preset": name, "output": (result.stderr or result.stdout).strip()[-300:]}


def deny(request_id: str, reason: str) -> dict:
    item = _get(request_id)
    conn = _db()
    try:
        conn.execute("UPDATE requests SET status = 'denied' WHERE id = ?", (request_id,))
        conn.commit()
    finally:
        conn.close()
    audit.record(kind="approval", verdict="denied", action="deny", sandbox=item["sandbox"],
                 detail={"request_id": request_id, "host": item["host"], "port": item["port"], "reason": reason})
    return {"request": item, "status": "denied"}


def main(args) -> int:
    assignments = load_assignments()
    nb = assignments.host.nemoclaw_bin
    runner = SubprocessRunner(cwd=bs.ROOT)
    if args.action == "sync":
        sandboxes = [args.sandbox] if args.sandbox else assignments.ordered_sandboxes()
        new = sync(runner, sandboxes, nb)
        print(json.dumps({"new": new, "pending": len(list_requests())}, ensure_ascii=False, indent=2))
        return 0
    if args.action == "list":
        for item in list_requests(None if args.sandbox == "all" else "pending"):
            print(json.dumps(item, ensure_ascii=False))
        return 0
    if not args.request_id:
        print("error: request_id required")
        return 2
    try:
        if args.action == "approve":
            print(json.dumps(approve(args.request_id, args.reason, runner, nb, assignments.host.lan_ip), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(deny(args.request_id, args.reason), ensure_ascii=False, indent=2))
    except KeyError:
        print(f"error: unknown request {args.request_id}")
        return 2
    return 0


__all__ = ["main", "sync", "approve", "deny", "list_requests", "parse_denied", "Path"]
