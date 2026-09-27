import json, httpx
base="http://127.0.0.1:8780"
c=httpx.Client(base_url=base, headers={"Origin":base}, timeout=120)
c.headers["X-RFA-CSRF"]=c.get("/ui/api/csrf").json()["csrf_token"]
sid=json.load(open(".agent/evidence/poc-task-seed-20260927/result.json"))["session_id"]
text="[샘플] 양자화 INT4 논문 근거를 조사해줘 (합성 시연용)"
t=c.post(f"/ui/api/sessions/{sid}/chat", json={"text":text,"message_id":"seed-task-quant-research-03"}).json()
after=c.get("/ui/api/chat/assignees").json()
q1=c.post(f"/ui/api/sessions/{sid}/chat", json={"text":"양자화 INT4 논문 근거 알려줘","message_id":"seed-follow-04"}).json()
q2=c.post(f"/ui/api/sessions/{sid}/chat", json={"text":"안드로메다 점심 메뉴 알려줘","message_id":"seed-follow-05"}).json()
print(json.dumps({"seeded":{"route":t["route"],"team":t.get("team"),"stages":[s["label"] for s in t["stages"]]},"after":after,"follow_quant":q1["route"],"follow_nomatch":q2["route"],"note":"first attempt seed-task-quant-research-01 was routed to the aurora task by shared [샘플]/합성/시연용 tags before commit 7ebcffd; preserved as a real observation"},ensure_ascii=False,indent=1))
