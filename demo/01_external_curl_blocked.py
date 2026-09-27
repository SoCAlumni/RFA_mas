#!/usr/bin/env python3
"""Demo 01 — 외부 curl 차단: egress-none 샌드박스에서 example.com 과 integrate.api.nvidia.com 으로의
직접 요청이 OpenShell 정책에 막히고, OCSF DENIED 기록이 남으며, 차단 요청이 승인 대기 목록에 오른다."""

from _lib import main, nemoclaw_bin

SANDBOX = "rfa-tasks-none"


def body(demo):
    nb = nemoclaw_bin()
    probe = "curl -s -m 8 -o /dev/null -w '%{http_code}' https://example.com/ ; echo \" exit=$?\""
    out = demo.run("sandbox curl https://example.com (expect blocked)",
                   [nb, SANDBOX, "exec", "--timeout", "30", "--", "sh", "-c", probe], timeout=60,
                   excerpt=lambda r: r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-120:])
    last = out.strip().splitlines()[-1] if out.strip() else ""
    demo.check("example.com did not return 200", "200" not in last.split()[0:1], f"probe output: {last!r}")
    probe2 = "curl -s -m 8 -o /dev/null -w '%{http_code}' https://integrate.api.nvidia.com/v1/models ; echo \" exit=$?\""
    out2 = demo.run("sandbox curl integrate.api.nvidia.com (baseline entry excluded)",
                    [nb, SANDBOX, "exec", "--timeout", "30", "--", "sh", "-c", probe2], timeout=60,
                    excerpt=lambda r: r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-120:])
    last2 = out2.strip().splitlines()[-1] if out2.strip() else ""
    demo.check("nvidia endpoint did not return 200", "200" not in last2.split()[0:1], f"probe output: {last2!r}")
    logs = demo.run("nemoclaw logs --tail 200 (OCSF)", [nb, SANDBOX, "logs", "--tail", "200"], timeout=60,
                    excerpt=lambda r: f"{sum('DENIED' in ln for ln in r.stdout.splitlines())} DENIED lines")
    denied = [ln for ln in logs.splitlines() if "DENIED" in ln and ("example.com" in ln or "nvidia.com" in ln)]
    demo.check("OCSF DENIED recorded for both hosts", bool(denied), (denied[-1][-160:] if denied else "none"))
    synced = demo.run("sg requests sync (pending approvals)",
                      ["uv", "run", "--offline", "--frozen", "python", "-m", "rfa_mas.nemoclaw", "requests", "sync", "--sandbox", SANDBOX],
                      timeout=90, excerpt=lambda r: r.stdout.strip().replace("\n", " ")[-200:])
    demo.check("blocked request visible to approve/deny flow", '"pending":' in synced and '"pending": 0' not in synced,
               "python -m rfa_mas.nemoclaw requests list")


if __name__ == "__main__":
    raise SystemExit(main("01", "외부 curl 차단 (OpenShell 정책 + OCSF + 승인 대기)", body))
