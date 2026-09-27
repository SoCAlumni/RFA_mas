#!/usr/bin/env python3
"""Demo 08 — 에이전트 격하 이동 시 워크스페이스 스캔: research(intranet-ro, privilege 1) 의 워크스페이스에 내부 수치가
든 메모를 만든 뒤 egress-none(privilege 0) 으로 격하 이동하면 브로커 drain → 번들 export → censor 스캔(redact)
→ agents apply(양쪽) → 마스킹된 상태만 import 된다. 마지막에 원래 그룹으로 격상 이동(상태 포함)해 복구한다."""

import tempfile
from pathlib import Path

from _lib import main, nemoclaw_bin

AGENT = "research"
FROM, TO = "rfa-tasks-intranet", "rfa-tasks-none"
SG = ["uv", "run", "--offline", "--frozen", "python", "-m", "rfa_mas.nemoclaw"]
NOTE = "오로라 벤치마크 메모: P95 12.5 ms, 예산 1,200,000 원, 담당 lead@example.com\n"


def body(demo):
    nb = nemoclaw_bin()
    with tempfile.TemporaryDirectory() as tmp:
        note = Path(tmp) / "memo.md"
        note.write_text(NOTE, encoding="utf-8")
        demo.run("seed internal note into research workspace", [nb, FROM, "upload", str(note), f"/sandbox/.openclaw/workspace-{AGENT}/memo.md"],
                 timeout=90, excerpt=lambda r: "uploaded")
    out = demo.run("sg relocate research --to-groups egress-none (demote → drain, scan, agents apply)",
                   SG + ["relocate", AGENT, "--to-groups", "egress-none"], timeout=240,
                   excerpt=lambda r: " | ".join(ln for ln in r.stdout.splitlines() if ln.startswith(("relocation plan", "workspace scan", "relocated", "drain")))[-400:])
    demo.check("direction demote with workspace scan", "'direction': 'demote'" in out and "workspace scan" in out, "see relocate output")
    demo.check("scan redacted the internal note", "'redactions': " in out and "'redactions': 0" not in out, out.split("workspace scan:")[-1][:160] if "workspace scan:" in out else "")
    listed = demo.run("agents list in target sandbox", [nb, TO, "agents", "list", "--json"], timeout=60, excerpt=lambda r: r.stdout[-200:])
    demo.check("research now lives in rfa-tasks-none", f'"id": "{AGENT}"' in listed or f'"id":"{AGENT}"' in listed, "roster")
    memo = demo.run("carried memo is masked", [nb, TO, "exec", "--timeout", "30", "--", "cat", f"/sandbox/.openclaw/workspace-{AGENT}/memo.md"],
                    timeout=60, excerpt=lambda r: r.stdout.strip()[-200:])
    demo.check("no raw figures in the carried memo", "12.5" not in memo and "1,200,000" not in memo and "[REDACTED" in memo, memo.strip()[-160:])
    back = demo.run("sg relocate research --to-groups intranet-ro (promote, state carried)",
                    SG + ["relocate", AGENT, "--to-groups", "intranet-ro"], timeout=240,
                    excerpt=lambda r: " | ".join(ln for ln in r.stdout.splitlines() if ln.startswith(("relocation plan", "relocated")))[-300:])
    demo.check("promoted back with state", "'direction': 'promote'" in back and "relocated" in back, "ok")
    demo.run("cleanup memo", [nb, FROM, "exec", "--timeout", "30", "--", "rm", "-f", f"/sandbox/.openclaw/workspace-{AGENT}/memo.md"],
             timeout=60, excerpt=lambda r: "removed")


if __name__ == "__main__":
    raise SystemExit(main("08", "에이전트 격하 이동 시 워크스페이스 스캔 (drain → scan → agents apply)", body))
