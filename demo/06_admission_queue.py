#!/usr/bin/env python3
"""Demo 06 — admission queue: `/ask` 는 max_inflight(ask.yaml) 만큼만 동시에 처리한다. 두 요청을 동시에 보내면 두 번째는
즉시 `202 {status: queued, position}` 을 받고 `GET /ask/{request_id}` 로 폴링해 200 을 받는다. 같은 request_id 재요청은 캐시 응답."""

import concurrent.futures as cf
import time

import httpx
from _lib import ENTRY, ask_token, main

Q1 = "오로라 벤치마크 절차와 로그 형식을 알려줘"
Q2 = "네뷸라 양자화 실험 계획을 알려줘"


def body(demo):
    headers = {"Authorization": f"Bearer {ask_token()}"}
    stamp = int(time.time())
    ids = [f"demo06-a-{stamp}", f"demo06-b-{stamp}"]

    def post(rid, q):
        started = time.monotonic()
        r = httpx.post(f"{ENTRY}/ask", json={"request_id": rid, "question": q, "channel": "slack", "audience": "company",
                                              "target": "demo06"}, headers=headers, timeout=400)
        return rid, r.status_code, r.json(), int((time.monotonic() - started) * 1000)

    with cf.ThreadPoolExecutor(2) as pool:
        first = pool.submit(post, ids[0], Q1)
        time.sleep(0.3)
        second = pool.submit(post, ids[1], Q2)
        rid2, status2, data2, ms2 = second.result()
        demo.step("B: second concurrent /ask answered immediately", status2 == 202,
                  f"HTTP {status2} {data2} ({ms2}ms)", ms=ms2)
        demo.check("B: 202 queued, position 1", status2 == 202 and data2.get("status") == "queued" and data2.get("position") == 1, str(data2))
        polls, status = 0, 202
        deadline = time.monotonic() + 380
        while status == 202 and time.monotonic() < deadline:
            time.sleep(2)
            polls += 1
            r = httpx.get(f"{ENTRY}/ask/{rid2}", headers=headers, timeout=30)
            status, data2 = r.status_code, r.json()
        rid1, status1, data1, ms1 = first.result()
    demo.step("A: first /ask processed inline", status1 == 200, f"HTTP {status1} verdict={(data1.get('censor') or {}).get('verdict')} "
              f"refusal={(data1.get('refusal') or {}).get('code')} ({ms1}ms)", ms=ms1)
    demo.check("B: polling GET /ask/{id} ended in 200 with the queued request's answer",
               status == 200 and data2.get("request_id") == rid2, f"polls={polls} refusal={(data2.get('refusal') or {}).get('code')}")
    again = demo.http("A again: same request_id → cached 200", "POST", f"{ENTRY}/ask",
                      json_body={"request_id": ids[0], "question": "다른 질문", "channel": "slack", "audience": "company", "target": "demo06"},
                      headers=headers, timeout=30, summarize=lambda d: f"request_id={d.get('request_id')} same={d == data1}")
    demo.check("idempotent: identical payload, no second sandbox turn", again == data1, "cache hit")


if __name__ == "__main__":
    raise SystemExit(main("06", "admission queue: 202 queued → GET 폴링 → 200; request_id 멱등", body, budget=600))
