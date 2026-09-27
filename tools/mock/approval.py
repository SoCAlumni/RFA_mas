#!/usr/bin/env python3
"""Mock of the counterpart's approval server (A). Single-file FastAPI, in-memory.

  serve   python tools/mock/approval.py serve --port 8811 [--auto reject-if-regex '20\\d\\d-\\d\\d-\\d\\d|intra\\.local']
  CLI     tools/mock/rfa-mock list [--url ...] | approve <id> --reason ... | reject <id> --reason ...

POST /approvals            submit a draft → {id, status: pending|approved|rejected, reason}
GET  /approvals[?status=]  list
GET  /approvals/{id}       one
POST /approvals/{id}/approve | /reject {reason}
"publishing" is a log line. Auto mode decides at submit time; manual mode leaves drafts pending
until the CLI decides. The auto reason names the offending match so the knowledge server's
feedback loop has something concrete to learn from.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import time

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

DEFAULT_REGEX = r"20\d\d-\d\d-\d\d|https?://[^\s]*(?:intra|internal|corp)\.[^\s]*|(?:10|192\.168)\.\d+\.\d+\.\d+"


class Submit(BaseModel):
    request_id: str
    target: str = ""
    audience: str = "public"
    channel: str = ""
    draft: str = Field(min_length=1)


class Decision(BaseModel):
    reason: str = Field(default="", max_length=1000)


def create_app(auto_regex: str | None = None) -> FastAPI:
    app = FastAPI(title="mock approval (A)", version="0.1.0")
    store: dict[str, dict] = {}
    seq = itertools.count(1)
    pattern = re.compile(auto_regex) if auto_regex else None

    def log(item: dict, event: str) -> None:
        print(f"[approval] {event} id={item['id']} request_id={item['request_id']} target={item['target']} "
              f"status={item['status']} reason={item.get('reason') or '-'}", flush=True)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "mock-approval", "auto": bool(pattern), "pending": sum(1 for i in store.values() if i["status"] == "pending")}

    @app.post("/approvals", status_code=201)
    async def submit(body: Submit):
        item = {"id": f"apr-{next(seq):04d}", "status": "pending", "reason": None, "created": time.time(),
                **body.model_dump()}
        if pattern is not None:
            m = pattern.search(body.draft)
            if m:
                item["status"], item["reason"] = "rejected", f"게시 불가 내용 포함: '{m.group(0)}' (자동 판정 규칙)"
            else:
                item["status"] = "approved"
        store[item["id"]] = item
        log(item, "submitted" if item["status"] == "pending" else f"auto-{item['status']}")
        if item["status"] == "approved":
            print(f"[approval] PUBLISH ({item['channel']} {item['target']}):\n{item['draft'][:400]}", flush=True)
        return item

    @app.get("/approvals")
    async def list_all(status: str | None = None):
        items = [i for i in store.values() if status is None or i["status"] == status]
        return {"approvals": sorted(items, key=lambda i: i["id"])}

    @app.get("/approvals/{item_id}")
    async def get_one(item_id: str):
        item = store.get(item_id)
        if item is None:
            return JSONResponse({"code": "not_found"}, status_code=404)
        return item

    @app.post("/approvals/{item_id}/approve")
    async def approve(item_id: str, body: Decision | None = None):
        item = store.get(item_id)
        if item is None:
            return JSONResponse({"code": "not_found"}, status_code=404)
        item["status"], item["reason"] = "approved", (body.reason if body else "") or None
        log(item, "approved")
        print(f"[approval] PUBLISH ({item['channel']} {item['target']}):\n{item['draft'][:400]}", flush=True)
        return item

    @app.post("/approvals/{item_id}/reject")
    async def reject(item_id: str, body: Decision):
        item = store.get(item_id)
        if item is None:
            return JSONResponse({"code": "not_found"}, status_code=404)
        if not body.reason.strip():
            return JSONResponse({"code": "reason_required"}, status_code=422)
        item["status"], item["reason"] = "rejected", body.reason.strip()
        log(item, "rejected")
        return item

    return app


# ---- CLI ---------------------------------------------------------------------------------------


def _cli(args) -> int:
    base = args.url.rstrip("/")
    with httpx.Client(timeout=10) as client:
        if args.command == "list":
            r = client.get(f"{base}/approvals", params={"status": args.status} if args.status else None)
            for item in r.json().get("approvals", []):
                print(f"{item['id']}  {item['status']:<9} {item['request_id']:<24} {item['target']}  {(item.get('reason') or '')[:60]}")
            return 0
        r = client.post(f"{base}/approvals/{args.id}/{args.command}", json={"reason": args.reason or ""})
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        return 0 if r.status_code == 200 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8811)
    s.add_argument("--auto", nargs="?", const=f"reject-if-regex {DEFAULT_REGEX}",
                   help="auto decide: 'reject-if-regex <regex>' (default regex: dates + internal hosts)")
    for name in ("list", "approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("--url", default="http://127.0.0.1:8811")
        if name == "list":
            p.add_argument("--status")
        else:
            p.add_argument("id")
            p.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        regex = None
        if args.auto:
            mode, _, value = args.auto.partition(" ")
            if mode != "reject-if-regex":
                print("error: --auto supports only 'reject-if-regex <regex>'", file=sys.stderr)
                return 2
            regex = value or DEFAULT_REGEX
        print(f"[approval] mock approval server http://{args.host}:{args.port}  auto={'reject-if-regex' if regex else 'manual'}", flush=True)
        uvicorn.run(create_app(regex), host=args.host, port=args.port, log_level="warning")
        return 0
    return _cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
