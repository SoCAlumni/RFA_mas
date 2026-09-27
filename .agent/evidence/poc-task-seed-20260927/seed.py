import json, httpx
base="http://127.0.0.1:8780"
c=httpx.Client(base_url=base, headers={"Origin":base}, timeout=120)
c.headers["X-RFA-CSRF"]=c.get("/ui/api/csrf").json()["csrf_token"]
before=c.get("/ui/api/chat/assignees").json()
sid=c.post("/ui/api/sessions",json={}).json()["session_id"]
seeds=[
 ("[샘플] 오로라 TRIV3 지연 벤치마크 결과를 검증해줘 (합성 시연용)", "seed-task-aurora-benchmark-01"),
 ("[샘플] 양자화 INT4 논문 근거를 조사해줘 (합성 시연용)", "seed-task-quant-research-01"),
]
out=[]
for text,key in seeds:
    r=c.post(f"/ui/api/sessions/{sid}/chat", json={"text":text,"message_id":key})
    r.raise_for_status(); t=r.json()
    out.append({"message_id":key,"intent":t["intent"],"route":t["route"],"team":t.get("team"),"stages":[s["label"] for s in t["stages"]]})
    # idempotent replay: same key -> same run, no second team
    again=c.post(f"/ui/api/sessions/{sid}/chat", json={"text":text,"message_id":key}).json()
    assert again["run_id"]==t["run_id"]
after=c.get("/ui/api/chat/assignees").json()
follow=c.post(f"/ui/api/sessions/{sid}/chat", json={"text":"오로라 지연 벤치마크 결과 알려줘","message_id":"seed-follow-01"}).json()
result={"before":before,"seeded":out,"after":after,"follow_route":follow["route"],"session_id":sid,"mode":"local/mock synthetic; simulated experiment values"}
print(json.dumps(result,ensure_ascii=False,indent=1))
