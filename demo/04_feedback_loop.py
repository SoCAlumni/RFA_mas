#!/usr/bin/env python3
"""Demo 04 — 되먹임: 목업 desk 가 시나리오 ④를 라이브 `/ask` 에 보낸다. 1라운드 답변의 사내 주소를 자동 결재(reject-if-regex)가
거절하면 desk 가 `feedback[]` 를 붙여 재요청하고, 지식 서버는 사유를 `censor-rules/learned.yaml` 에 누적해 검열 LLM 단계와
head 에 주입한다 → 2라운드에서 같은 사유가 재발하지 않는다. 승인 전 초안이 기밀 영역을 나가는 일은 없고, 나가는 것은 검열을 통과한 텍스트뿐이다."""

import sys
import time

import yaml
from _lib import ENTRY, ROOT, ask_token, main

sys.path.insert(0, str(ROOT / "tools" / "mock"))
from approval import DEFAULT_REGEX, create_app  # noqa: E402
from desk import Desk, load_scenarios  # noqa: E402
from run_e2e import Background, free_port  # noqa: E402

LEARNED = ROOT / "deploy" / "nemoclaw" / "censor-rules" / "learned.yaml"


def body(demo):
    scenario = load_scenarios([str(ROOT / "tools" / "mock" / "scenarios" / "04_feedback_loop.yaml")])[0]
    before = len((yaml.safe_load(LEARNED.read_text(encoding="utf-8")) or {}).get("learned") or [])
    with Background(create_app(DEFAULT_REGEX), free_port()) as approval:
        demo.step("approval mock (auto reject-if-regex dates/internal hosts) up", True, approval.url)
        desk = Desk(ENTRY, ask_token(), approval.url, log=lambda m: demo.say("  " + m))
        started = time.monotonic()
        outcome = desk.run(scenario, run_tag=f"demo04-{int(time.time())}")
        ms = int((time.monotonic() - started) * 1000)
    demo.step("desk: /ask → draft → approval → feedback → /ask", not outcome.error,
              f"rounds={len(outcome.rounds)} statuses={[(r.approval or {}).get('status') for r in outcome.rounds]} {ms}ms", ms=ms)
    r1 = outcome.rounds[0]
    demo.check("round 1 rejected by the reviewer for an internal address",
               (r1.approval or {}).get("status") == "rejected" and "intra" in str((r1.approval or {}).get("reason")),
               str((r1.approval or {}).get("reason"))[:160])
    after = yaml.safe_load(LEARNED.read_text(encoding="utf-8")) or {}
    rules = after.get("learned") or []
    demo.check("reason appended to censor-rules/learned.yaml {audience, task, reason, at}",
               len(rules) > before and rules[-1]["audience"] == "public" and rules[-1]["task"] == "triv3",
               f"{len(rules)} rules; last={rules[-1]['reason'][:80] if rules else None}")
    fails = outcome.check()
    last = outcome.rounds[-1]
    demo.check("round 2 approved; same reason did not recur; address absent from knowledge", not fails,
               f"verdict={outcome.verdict} approval={outcome.approval_status} fails={fails}")
    events = demo.http("audit: second /ask carried the learned hint into the censor", "GET",
                       f"{ENTRY}/audit/api/events?kind=ask&session_id=ask-{last.request_id}", timeout=10,
                       summarize=lambda d: str((d.get("events") or [{}])[0].get("detail", {}).get("hints")))
    detail = (events.get("events") or [{}])[0].get("detail", {})
    demo.check("audit detail: hints ≥ 1, learned_added recorded", detail.get("hints", 0) >= 1, str({k: detail.get(k) for k in ("hints", "learned_added", "head")}))


if __name__ == "__main__":
    raise SystemExit(main("04", "되먹임: 거절 사유 → learned.yaml → 다음 /ask 의 검열·head 에 반영", body, budget=600))
