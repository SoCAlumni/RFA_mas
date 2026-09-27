#!/usr/bin/env python3
"""Demo 02 — 보안 그룹 변경 → 정책 반영: assignments.yaml 의 egress-none 그룹에 sg-intranet-ro preset 을
추가하고 `sg apply` 하면 rfa-tasks-none 이 사내 API /healthz 에 닿고(policy add), 되돌리면 다시 막힌다(policy remove).
변경은 항상 `nemoclaw policy add/remove` 로만 반영되며 파일은 실행 후 원래대로 복구된다."""

import re
from pathlib import Path

from _lib import ROOT, main, nemoclaw_bin

SANDBOX = "rfa-tasks-none"
ASSIGNMENTS = ROOT / "deploy" / "nemoclaw" / "assignments.yaml"
SG = ["uv", "run", "--offline", "--frozen", "python", "-m", "rfa_mas.nemoclaw"]
PROBE = "curl -s -m 8 -o /dev/null -w '%{http_code}' http://192.168.123.191:8791/healthz ; echo \" exit=$?\""


def set_presets(text: str, presets: str) -> str:
    return re.sub(r"(?m)^(  egress-none:\n(?:    .*\n)*?    presets: )\[.*\]$", rf"\g<1>[{presets}]", text, count=1)


def body(demo):
    nb = nemoclaw_bin()
    original = ASSIGNMENTS.read_text(encoding="utf-8")
    last = lambda r: (r.stdout.strip().splitlines() or [r.stderr[-120:]])[-1]
    try:
        before = demo.run("before: sandbox → intranet /healthz (expect denied)",
                          [nb, SANDBOX, "exec", "--timeout", "30", "--", "sh", "-c", PROBE], timeout=60, excerpt=last)
        demo.check("denied before the change", "200" not in before, before.strip().splitlines()[-1] if before.strip() else "")
        ASSIGNMENTS.write_text(set_presets(original, "sg-intranet-ro"), encoding="utf-8")
        demo.step("assignments.yaml: egress-none.presets += sg-intranet-ro", True, "declarative change only")
        plan = demo.run("sg plan --sandbox rfa-tasks-none", SG + ["plan", "--sandbox", SANDBOX, "--skip-mcp"], timeout=120,
                        excerpt=lambda r: " | ".join(ln.split("  #")[0] for ln in r.stdout.splitlines() if ln.startswith("["))[-300:])
        demo.check("plan issues policy add (never openshell policy set)", "[policy-add]" in plan and "policy set" not in plan, "ok")
        demo.run("sg apply (nemoclaw policy add --from-file)", SG + ["apply", "--sandbox", SANDBOX, "--skip-mcp"], timeout=180,
                 excerpt=lambda r: " | ".join(ln for ln in r.stdout.splitlines() if ln.startswith(("ok", "failed")))[-300:])
        after = demo.run("after: sandbox → intranet /healthz (expect 200)",
                         [nb, SANDBOX, "exec", "--timeout", "30", "--", "sh", "-c", PROBE], timeout=60, excerpt=last)
        demo.check("allowed after the change", after.strip().splitlines()[-1].startswith("200") if after.strip() else False,
                   after.strip().splitlines()[-1] if after.strip() else "")
        policy = demo.run("nemoclaw policy get shows the preset", [nb, SANDBOX, "policy", "get"], timeout=60,
                          excerpt=lambda r: ", ".join(sorted({ln.strip().rstrip(':') for ln in r.stdout.splitlines() if 'nemoclaw_custom__' in ln}))[-200:])
        demo.check("live policy contains nemoclaw_custom__sg-intranet-ro", "nemoclaw_custom__sg-intranet-ro" in policy, "ok")
    finally:
        ASSIGNMENTS.write_text(original, encoding="utf-8")
    demo.step("assignments.yaml restored", True, "revert the declaration")
    demo.run("sg apply (nemoclaw policy remove)", SG + ["apply", "--sandbox", SANDBOX, "--skip-mcp"], timeout=180,
             excerpt=lambda r: " | ".join(ln for ln in r.stdout.splitlines() if ln.startswith(("ok", "failed")))[-300:])
    final = demo.run("revert: sandbox → intranet /healthz (expect denied)",
                     [nb, SANDBOX, "exec", "--timeout", "30", "--", "sh", "-c", PROBE], timeout=60, excerpt=last)
    demo.check("denied again after revert", "200" not in final, final.strip().splitlines()[-1] if final.strip() else "")


if __name__ == "__main__":
    raise SystemExit(main("02", "보안 그룹 변경 → nemoclaw policy add/remove 반영", body))
