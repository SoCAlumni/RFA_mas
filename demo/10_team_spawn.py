#!/usr/bin/env python3
"""Demo 10 — 팀 스폰 API: 과제 이름·설명(자연어)을 `POST /teams` 에 주면 승인된 역할 카탈로그 안에서 패터닝해 기본 샌드박스에
supervisor(task 대표) + 멤버(research/benchmark/summarizer/verifier) 팀을 선언·적용·시드하고 `/ask` 카탈로그에 올린다.
같은 질문을 `/ask` 로 보내면 head 가 새 팀의 task 를 고르고 브로커가 supervisor 를 부른다. 마지막에 팀을 제거해 복구한다."""

import time

from _lib import ENTRY, ask_token, main

NAME = "오로라 대시보드 문의 대응"
DESC = "오로라 벤치마크 수치와 사내 문서 근거를 찾아 파트너 질문에 답하고 요약 공지 초안을 만든다"
TASK_ID = f"demo10-{int(time.time()) % 100000}"


def body(demo):
    headers = {"Authorization": f"Bearer {ask_token()}"}
    created = demo.http("POST /teams (요구사항 → 패터닝 → 선언·적용·시드)", "POST", f"{ENTRY}/teams",
                        json_body={"task_id": TASK_ID, "name": NAME, "description": DESC}, headers=headers, timeout=900,
                        expect_status=201,
                        summarize=lambda d: f"status={d.get('status')} pattern={(d.get('pattern') or {}).get('roles')} "
                                            f"source={(d.get('pattern') or {}).get('source')} apply={(d.get('applied') or {}).get('agents_apply')}")
    team_id = created.get("team_id")
    members = [m["agent_id"] for m in created.get("members", [])]
    demo.check("supervisor + members from the approved catalogue, verifier always last",
               created.get("supervisor") == f"t-{TASK_ID}-sup" and members and members[-1].endswith("-verifier")
               and created.get("status") in ("ready", "failed"), f"supervisor={created.get('supervisor')} members={members}")
    demo.check("declared in teams.yaml and applied to the default sandbox (no new sandbox)",
               created.get("sandbox") == "rfa-main" and (created.get("applied") or {}).get("agents_apply") in ("ok", "skipped"),
               f"sandbox={created.get('sandbox')} applied={created.get('applied')}")
    try:
        asked = demo.http("POST /ask with a question for that task", "POST", f"{ENTRY}/ask",
                          json_body={"request_id": f"{TASK_ID}-q1", "question": f"{NAME} 방법을 알려줘", "channel": "github",
                                     "audience": "company", "target": "demo10"}, headers=headers, timeout=400, expect_status=(200, 202),
                          summarize=lambda d: f"task={(d.get('task') or {}).get('id')} refusal={(d.get('refusal') or {}).get('code')} "
                                              f"verdict={(d.get('censor') or {}).get('verdict')}")
        demo.check("head routed the question to the spawned team (task id = new team)",
                   (asked.get("task") or {}).get("id") == TASK_ID, str(asked.get("task")))
        events = demo.http("audit: kind=team create + kind=ask task", "GET", f"{ENTRY}/audit/api/events?kind=team&limit=1", timeout=10,
                           summarize=lambda d: str((d.get("events") or [{}])[0].get("detail", {}).get("pattern")))
        detail = (events.get("events") or [{}])[0].get("detail", {})
        demo.check("audit detail carries pattern source/roles and apply outcome, not the description text",
                   detail.get("team_id") == team_id and "pattern" in detail and "description" not in detail, str(detail)[:200])
    finally:
        demo.http("DELETE /teams/{id} (roster back to declared state)", "DELETE", f"{ENTRY}/teams/{team_id}", headers=headers,
                  timeout=900, expect_status=202, summarize=lambda d: f"status={d.get('status')}")


if __name__ == "__main__":
    raise SystemExit(main("10", "팀 스폰 API: 요구사항 → supervisor + 멤버 팀 → /ask 라우팅", body, budget=900))
