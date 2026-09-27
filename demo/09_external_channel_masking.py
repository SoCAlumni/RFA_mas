#!/usr/bin/env python3
"""Demo 09 — external 채널 응답 마스킹: (A) egress-proxy 가 external 마커가 붙은 요청을 hosted 모델로 보내며
요청·응답의 수치/키워드/이메일을 마스킹하고 감사 로그에 남긴다. (B) 채널 API 진입점으로 같은 질문을 보내면
assistant → broker → task 에이전트 경로의 최종 응답이 같은 censor 함수를 통과한다 (hosted 턴 90초 제한)."""

import json

from _lib import ROOT, main, marker_secret, proxy_key

PROXY = "http://127.0.0.1:8797/v1/chat/completions"
ENTRY = "http://127.0.0.1:8799"
QUESTION = "오로라 프로젝트의 P95 지연 12.5 ms 와 예산 1,200,000 원을 기준으로 lead@example.com 에게 보낼 요약을 써줘."


def body(demo):
    from rfa_mas.nemoclaw.markers import make_marker

    key, secret = proxy_key(), marker_secret()
    marker = make_marker("channel", {"ch": "external", "sid": "demo03"}, secret)
    data = demo.http("A: proxy request with external channel marker → hosted model", "POST", PROXY,
                     json_body={"model": "rfa-auto", "max_tokens": 200,
                                "messages": [{"role": "user", "content": marker + "\n" + QUESTION}]},
                     headers={"Authorization": f"Bearer {key}"}, timeout=90,
                     summarize=lambda d: (d.get("choices") or [{}])[0].get("message", {}).get("content", str(d))[:200])
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    demo.check("response contains no raw figures/email", all(t not in content for t in ("12.5", "1,200,000", "lead@example.com")),
               content[:200])
    events = demo.http("audit: last inference event", "GET", f"{ENTRY}/audit/api/events?kind=inference&limit=1", timeout=10,
                       summarize=lambda d: json.dumps(d["events"][0]["detail"], ensure_ascii=False)[:250] if d.get("events") else "none")
    ev = (events.get("events") or [{}])[0]
    detail = ev.get("detail", {})
    demo.check("audit: external channel, alias rfa-external, redactions ≥ 2, verdict redact/allow",
               ev.get("channel") == "external" and detail.get("alias") == "rfa-external"
               and (detail.get("request_redactions", 0) + detail.get("response_redactions", 0)) >= 2 and ev.get("verdict") in ("redact", "allow"),
               f"channel={ev.get('channel')} alias={detail.get('alias')} req={detail.get('request_redactions')} resp={detail.get('response_redactions')} verdict={ev.get('verdict')}")
    turn = demo.http("B: channel API /channel/external/chat (assistant → broker → task agent)", "POST",
                     f"{ENTRY}/channel/external/chat", json_body={"text": QUESTION, "session_id": "demo03-e2e"},
                     timeout=95, expect_status=(201, 502),
                     summarize=lambda d: f"status={d.get('status')} verdict={d.get('verdict')} reply={str(d.get('reply'))[:160]}")
    reply = str(turn.get("reply", ""))
    demo.check("B: final reply passed the same censor (no raw figures) and status ok",
               turn.get("status") == "ok" and all(t not in reply for t in ("12.5", "1,200,000", "lead@example.com")),
               f"status={turn.get('status')} verdict={turn.get('verdict')} redactions={turn.get('redactions')}")


if __name__ == "__main__":
    raise SystemExit(main("09", "external 채널 응답에서 수치·키워드 마스킹 (proxy + 채널 API)", body))
