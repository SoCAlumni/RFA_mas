# ---- admission queue -----------------------------------------------------------------------------


async def test_second_request_is_queued_202_then_polls_to_200(tmp_path):
    deps = build_fake_deps(cfg.load_ask(), cfg.load_censors(), tmp_path / "learned.yaml", task_delay=0.4)
    service = AskService(deps, "tok")
    async with client(service) as c:
        first = asyncio.create_task(c.post("/ask", json=body(request_id="q1"), headers=H))
        await asyncio.sleep(0.05)
        second = await c.post("/ask", json=body(request_id="q2"), headers=H)
        assert second.status_code == 202 and second.json() == {"request_id": "q2", "status": "queued", "position": 1}
        polled = await c.get("/ask/q2", headers=H)
        assert polled.status_code == 202
        assert (await first).status_code == 200
        for _ in range(40):
            polled = await c.get("/ask/q2", headers=H)
            if polled.status_code == 200:
                break
            await asyncio.sleep(0.05)
    assert polled.status_code == 200 and polled.json()["request_id"] == "q2" and polled.json()["refusal"] is None


async def test_queue_full_is_a_refusal(tmp_path):
    config = cfg.load_ask()
    config = config.model_copy(update={"admission": config.admission.model_copy(update={"max_queue": 0})})
    deps = build_fake_deps(config, cfg.load_censors(), tmp_path / "learned.yaml", task_delay=0.3)
    async with client(AskService(deps, "tok")) as c:
        first = asyncio.create_task(c.post("/ask", json=body(request_id="f1"), headers=H))
        await asyncio.sleep(0.05)
        second = await c.post("/ask", json=body(request_id="f2"), headers=H)
        await first
    assert second.status_code == 200 and second.json()["refusal"]["code"] == "queue_full"


async def test_server_side_timeout_refuses_no_knowledge_timeout(tmp_path):
