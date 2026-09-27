#!/usr/bin/env python3
"""Demo 07 — task 에이전트 런타임 추가: assignments.yaml 에 task 에이전트 `notes` 를 선언하면 manifest 가
다시 생성되고 `nemoclaw rfa-tasks-none agents apply -f` 가 재빌드 없이 roster 를 맞춘다(추가 → 목록 확인 → 제거)."""

from _lib import ROOT, main, nemoclaw_bin

SANDBOX = "rfa-tasks-none"
ASSIGNMENTS = ROOT / "deploy" / "nemoclaw" / "assignments.yaml"
MANIFEST = ROOT / "deploy" / "nemoclaw" / "agents" / f"{SANDBOX}.agents.yaml"
SG = ["uv", "run", "--offline", "--frozen", "python", "-m", "rfa_mas.nemoclaw"]
NEW_AGENT = """  notes:
    kind: task
    groups: [egress-none]
    alias: rfa-internal
    skill: task-summarizer
    description: 데모 07 에서 런타임에 추가한 메모 정리 담당.
    tools: { allow: [read] }
"""


def ids(stdout: str) -> str:
    import json

    from rfa_mas.nemoclaw.runner import extract_json

    try:
        return ",".join(sorted(e["id"] for e in extract_json(stdout)))
    except Exception:
        return stdout[-120:]


def body(demo):
    nb = nemoclaw_bin()
    original = ASSIGNMENTS.read_text(encoding="utf-8")
    try:
        before = demo.run("agents list before", [nb, SANDBOX, "agents", "list", "--json"], timeout=60, excerpt=lambda r: ids(r.stdout))
        demo.check("notes not present yet", "notes" not in ids(before), ids(before))
        ASSIGNMENTS.write_text(original.rstrip("\n") + "\n" + NEW_AGENT, encoding="utf-8")
        demo.run("sg render (manifest regenerated)", SG + ["render"], timeout=60,
                 excerpt=lambda r: "notes in manifest" if "notes" in MANIFEST.read_text() else "manifest missing notes")
        demo.run("nemoclaw agents apply -f (add)", [nb, SANDBOX, "agents", "apply", "-f", str(MANIFEST), "--yes", "--non-interactive"],
                 timeout=180, excerpt=lambda r: (r.stdout.strip().splitlines() or [r.stderr[-160:]])[-1][:200])
        after = demo.run("agents list after add", [nb, SANDBOX, "agents", "list", "--json"], timeout=60, excerpt=lambda r: ids(r.stdout))
        demo.check("notes is live in the sandbox roster", "notes" in ids(after), ids(after))
    finally:
        ASSIGNMENTS.write_text(original, encoding="utf-8")
    demo.run("sg render (declaration reverted)", SG + ["render"], timeout=60, excerpt=lambda r: "manifest without notes")
    demo.run("nemoclaw agents apply -f (remove orphan)", [nb, SANDBOX, "agents", "apply", "-f", str(MANIFEST), "--yes", "--non-interactive"],
             timeout=180, excerpt=lambda r: (r.stdout.strip().splitlines() or [r.stderr[-160:]])[-1][:200])
    final = demo.run("agents list after remove", [nb, SANDBOX, "agents", "list", "--json"], timeout=60, excerpt=lambda r: ids(r.stdout))
    demo.check("roster back to declared state", "notes" not in ids(final), ids(final))


if __name__ == "__main__":
    raise SystemExit(main("07", "task 에이전트 런타임 추가 (nemoclaw agents apply)", body))
