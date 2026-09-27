#!/usr/bin/env python3
"""Mock of the counterpart's desk / response agent (C).

Reads a scenario (question + thread) → ``POST /ask`` (202 → poll ``GET /ask/{id}``) → turns the
knowledge into a draft string with a template (no LLM) → submits to the approval mock → on
rejection re-asks with ``feedback[]`` (max 3 rounds). No knowledge of the server internals: the
contract is the only interface.

  python tools/mock/desk.py --ask-url http://127.0.0.1:8799 --token-env RFA_ASK_TOKEN \
         --approval-url http://127.0.0.1:8811 tools/mock/scenarios/*.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

MAX_ROUNDS = 3
DRAFT_TEMPLATE = "[{channel} 답변 초안 · {target}]\n{knowledge}\n\n— 근거: 사내 지식({task_name}). 자동 검열 {verdict}, 마스킹 {n_redactions}건."


@dataclass
class Round:
    request_id: str
    http_status: int
    queued_polls: int
    response: dict
    approval: dict | None
    ms: int


@dataclass
class Outcome:
    scenario: dict
    rounds: list[Round] = field(default_factory=list)
    error: str | None = None

    @property
    def last(self) -> Round | None:
        return self.rounds[-1] if self.rounds else None

    @property
    def final_response(self) -> dict:
        return self.last.response if self.last else {}

    @property
    def refusal(self) -> str | None:
        r = self.final_response.get("refusal")
        return r["code"] if r else None

    @property
    def verdict(self) -> str | None:
        return (self.final_response.get("censor") or {}).get("verdict")

    @property
    def approval_status(self) -> str | None:
        return (self.last.approval or {}).get("status") if self.last else None

    @property
    def total_ms(self) -> int:
        return sum(r.ms for r in self.rounds)

    def check(self, learned_reasons: list[str] | None = None) -> list[str]:
        """Return the list of failed expectations (empty = pass)."""
        exp = self.scenario.get("expect") or {}
        fails: list[str] = []
        if self.error:
            return [f"error: {self.error}"]
        if "refusal" in exp and self.refusal != exp["refusal"]:
            fails.append(f"refusal={self.refusal!r} expected {exp['refusal']!r}")
        if "verdict" in exp and self.verdict not in exp["verdict"]:
            fails.append(f"verdict={self.verdict!r} expected one of {exp['verdict']}")
        if "approval" in exp and self.approval_status != exp["approval"]:
            fails.append(f"approval={self.approval_status!r} expected {exp['approval']!r}")
        if "rounds" in exp and len(self.rounds) != exp["rounds"]:
            fails.append(f"rounds={len(self.rounds)} expected {exp['rounds']}")
        if exp.get("first_round_rejected") and (not self.rounds or (self.rounds[0].approval or {}).get("status") != "rejected"):
            fails.append("first round was not rejected")
        knowledge = self.final_response.get("knowledge") or ""
        for needle in exp.get("knowledge_excludes", []):
            if needle in knowledge:
                fails.append(f"knowledge contains {needle!r}")
        reasons = [r["reason"] for r in (self.final_response.get("censor") or {}).get("redactions", [])]
        for r in exp.get("redaction_reasons_include", []):
            if r not in reasons:
                fails.append(f"redaction reason {r!r} missing (got {reasons})")
        for r in exp.get("redaction_reasons_exclude", []):
            if r in reasons:
                fails.append(f"redaction reason {r!r} present")
        if "learned_reason_reused" in exp and len(self.rounds) >= 2:
            first_reason = (self.rounds[0].approval or {}).get("reason")
            later = [(r.approval or {}).get("reason") for r in self.rounds[1:]]
            reused = first_reason is not None and first_reason in later
            if reused != exp["learned_reason_reused"]:
                fails.append(f"same rejection reason recurred: {first_reason!r}")
        return fails


class Desk:
    def __init__(self, ask_url: str, token: str, approval_url: str, *, poll_interval: float = 0.5,
                 poll_timeout: float = 300.0, decision_timeout: float = 600.0, log=print):
        self.ask_url, self.token, self.approval_url = ask_url.rstrip("/"), token, approval_url.rstrip("/")
        self.poll_interval, self.poll_timeout, self.decision_timeout = poll_interval, poll_timeout, decision_timeout
        self.log = log
        self.client = httpx.Client(timeout=max(poll_timeout, 30.0) + 30)

    # ---- /ask -----------------------------------------------------------------------------------

    def ask(self, body: dict) -> tuple[int, int, dict]:
        headers = {"Authorization": f"Bearer {self.token}"}
        r = self.client.post(f"{self.ask_url}/ask", json=body, headers=headers)
        polls = 0
        if r.status_code == 202:
            deadline = time.monotonic() + self.poll_timeout
            self.log(f"    202 queued position={r.json().get('position')} → polling")
            while time.monotonic() < deadline:
                time.sleep(self.poll_interval)
                polls += 1
                r = self.client.get(f"{self.ask_url}/ask/{body['request_id']}", headers=headers)
                if r.status_code != 202:
                    break
        try:
            data = r.json()
        except ValueError:
            data = {"code": "non_json", "text": r.text[:200]}
        return r.status_code, polls, data

    # ---- approval --------------------------------------------------------------------------------

    def submit(self, scenario: dict, request_id: str, draft: str) -> dict:
        r = self.client.post(f"{self.approval_url}/approvals",
                             json={"request_id": request_id, "target": scenario.get("target", ""),
                                   "audience": scenario["audience"], "channel": scenario["channel"], "draft": draft})
        item = r.json()
        deadline = time.monotonic() + self.decision_timeout
        announced = False
        while item.get("status") == "pending" and time.monotonic() < deadline:
            if not announced:
                self.log(f"    approval {item['id']} pending — decide with: tools/mock/rfa-mock approve|reject {item['id']} --reason ...")
                announced = True
            time.sleep(1.0)
            item = self.client.get(f"{self.approval_url}/approvals/{item['id']}").json()
        return item

    # ---- one scenario ----------------------------------------------------------------------------

    @staticmethod
    def draft_for(scenario: dict, response: dict) -> str:
        censor = response.get("censor") or {}
        task = response.get("task") or {}
        return DRAFT_TEMPLATE.format(channel=scenario["channel"], target=scenario.get("target", ""),
                                     knowledge=response.get("knowledge", ""), task_name=task.get("name", "-"),
                                     verdict=censor.get("verdict"), n_redactions=len(censor.get("redactions", [])))

    def run(self, scenario: dict, run_tag: str | None = None, max_rounds: int = MAX_ROUNDS) -> Outcome:
        outcome = Outcome(scenario)
        feedback: list[dict] = []
        tag = run_tag or time.strftime("%H%M%S")
        self.log(f"== {scenario['id']}: {scenario.get('title', '')}")
        for n in range(1, max_rounds + 1):
            request_id = f"{scenario['id']}-{tag}-r{n}"
            body = {"request_id": request_id, "question": scenario["question"], "channel": scenario["channel"],
                    "audience": scenario["audience"], "target": scenario.get("target", ""), "url": scenario.get("url"),
                    "requester": scenario.get("requester"), "context": scenario.get("context", []), "feedback": feedback}
            started = time.monotonic()
            try:
                status, polls, data = self.ask(body)
            except httpx.HTTPError as exc:
                outcome.error = f"{type(exc).__name__}: {exc}"
                return outcome
            ms = int((time.monotonic() - started) * 1000)
            if status != 200:
                outcome.rounds.append(Round(request_id, status, polls, data, None, ms))
                outcome.error = f"HTTP {status}: {json.dumps(data, ensure_ascii=False)[:200]}"
                return outcome
            refusal = data.get("refusal")
            censor = data.get("censor") or {}
            self.log(f"  r{n} {request_id}: verdict={censor.get('verdict')} refusal={refusal['code'] if refusal else None} "
                     f"task={(data.get('task') or {}).get('id')} polls={polls} {ms}ms")
            if refusal:
                outcome.rounds.append(Round(request_id, status, polls, data, None, ms))
                self.log(f"    refusal: {refusal['code']} — {refusal['message']}")
                return outcome
            draft = self.draft_for(scenario, data)
            approval = self.submit(scenario, request_id, draft)
            ms = int((time.monotonic() - started) * 1000)
            outcome.rounds.append(Round(request_id, status, polls, data, approval, ms))
            self.log(f"    approval {approval.get('id')}: {approval.get('status')} {approval.get('reason') or ''}")
            if approval.get("status") == "approved":
                return outcome
            feedback.append({"draft": draft, "reason": approval.get("reason") or "rejected", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        self.log("    closed: 3 rejections")
        return outcome


def load_scenarios(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        data = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("id"):
            out.append(data)
    return out


def table(outcomes: list[Outcome]) -> str:
    rows = [("시나리오", "audience", "verdict", "refusal", "라운드", "결재", "소요(ms)", "결과")]
    for o in outcomes:
        fails = o.check()
        rows.append((o.scenario["id"], o.scenario["audience"], str(o.verdict), str(o.refusal), str(len(o.rounds)),
                     str(o.approval_status), str(o.total_ms), "PASS" if not fails else "FAIL: " + "; ".join(fails)))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    lines = []
    for i, r in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[j]) for j, c in enumerate(r[:-1])) + "  " + r[-1])
        if i == 0:
            lines.append("  ".join("-" * w for w in widths) + "  ----")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="+")
    parser.add_argument("--ask-url", default="http://127.0.0.1:8799")
    parser.add_argument("--token-env", default="RFA_ASK_TOKEN")
    parser.add_argument("--token", default=None, help="explicit token (prefer --token-env)")
    parser.add_argument("--approval-url", default="http://127.0.0.1:8811")
    parser.add_argument("--poll-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    token = args.token or os.environ.get(args.token_env) or ""
    if not token:
        print(f"error: token missing (set {args.token_env} or --token)", file=sys.stderr)
        return 2
    desk = Desk(args.ask_url, token, args.approval_url, poll_timeout=args.poll_timeout)
    outcomes = [desk.run(s) for s in load_scenarios(args.scenarios)]
    print()
    print(table(outcomes))
    return 0 if all(not o.check() for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
