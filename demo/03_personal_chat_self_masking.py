#!/usr/bin/env python3
"""Demo 03 — 개인 채팅 자동 마스킹(self): `POST /chat`(audience self)은 `/ask` 와 같은 `ask()` 를 호출한다. 사내 주소는
그대로 보이지만 개인정보(이메일)는 internal 프로파일이 자동 마스킹하고, 같은 질문을 audience public 으로 물으면 프로젝트명·
수치까지 public 프로파일로 마스킹된다. 감사 로그에는 두 요청이 kind=ask, profile 별로 남는다."""

from _lib import ENTRY, ask_token, main

QUESTION = "오로라 벤치마크 결과 대시보드는 어디서 보고 문의는 어디로 하나요"


def body(demo):
    chat = demo.http("A: POST /chat (audience self → profile internal)", "POST", f"{ENTRY}/chat",
                     json_body={"question": QUESTION, "session_id": "demo03"}, timeout=240,
                     summarize=lambda d: f"profile={(d.get('censor') or {}).get('profile')} verdict={(d.get('censor') or {}).get('verdict')} "
                                         f"knowledge={str(d.get('knowledge'))[:140]}")
    k = str(chat.get("knowledge", ""))
    demo.check("A: no refusal, e-mail masked, internal address kept for the owner",
               chat.get("refusal") is None and "[REDACTED:email]" in k and "@" not in k.replace("[REDACTED:email]", ""),
               f"redactions={[r['reason'] for r in (chat.get('censor') or {}).get('redactions', [])]}")
    pub = demo.http("B: POST /ask same question, audience public (bearer)", "POST", f"{ENTRY}/ask",
                    json_body={"request_id": f"demo03-public-{demo.started:.0f}", "question": QUESTION, "channel": "github",
                               "audience": "public", "target": "demo03"},
                    headers={"Authorization": f"Bearer {ask_token()}"}, timeout=240, expect_status=(200, 202),
                    summarize=lambda d: f"profile={(d.get('censor') or {}).get('profile')} verdict={(d.get('censor') or {}).get('verdict')} "
                                        f"refusal={(d.get('refusal') or {}).get('code')} knowledge={str(d.get('knowledge'))[:120]}")
    pk = str(pub.get("knowledge", ""))
    demo.check("B: public profile masks the project name and figures the owner saw in plain text",
               (pub.get("censor") or {}).get("profile") == "public" and "오로라" not in pk and "@" not in pk,
               f"redactions={[r['reason'] for r in (pub.get('censor') or {}).get('redactions', [])]}")
    events = demo.http("audit: kind=ask events", "GET", f"{ENTRY}/audit/api/events?kind=ask&limit=2", timeout=10,
                       summarize=lambda d: ", ".join(f"{e['profile']}/{e['verdict']}" for e in d.get("events", [])))
    profiles = {e.get("profile") for e in events.get("events", [])}
    demo.check("audit: both profiles recorded, no text bodies stored", profiles >= {"internal", "public"}
               and all("knowledge" not in e.get("detail", {}) for e in events.get("events", [])), str(profiles))


if __name__ == "__main__":
    raise SystemExit(main("03", "개인 채팅 자동 마스킹 (self → internal) vs 공개 (public)", body, budget=300))
