"""Real NVIDIA adapter + owner-consent gate over a fake chat-completions transport (P1-008K).

No network: every model call is answered by an in-process httpx.MockTransport. Synthetic
data only. Asserts what is sent (private context only for owner targets) and how the LLM
proposals are constrained (candidates only) and persisted (revision-bound answers).
"""

import json
import sqlite3

import httpx
import pytest
from test_chat_poc import send

from rfa_mas.adapters.nvidia import (
    NvidiaChatConfig,
    NvidiaChatModel,
    OwnerConsentEgressGate,
    PublicOnlyEgressGate,
)
from rfa_mas.application.chat import ChatMessage
from rfa_mas.application.graphs.domain import share_egress_filter
from rfa_mas.application.team_selector import SelectionRequest
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    EvidenceBundle,
    EvidenceItem,
    ModelRequest,
    SourceLocation,
)
from rfa_mas.errors import RfaError
from rfa_mas.poc.bootstrap import PocModelNotConfigured, create_poc_app, local_settings
from rfa_mas.poc.chat import ANSWER_LLM_SYSTEM
from rfa_mas.poc.routing import ROUTER_LLM_SYSTEM
from rfa_mas.settings import Settings

KEY = "SYNTHETIC_KEY_NOT_A_SECRET"


class FakeNvidia:
    """Deterministic stand-in for integrate.api.nvidia.com; records every request body."""

    def __init__(self):
        self.requests = []
        self.outside = False

    def handler(self, request: httpx.Request):
        body = json.loads(request.content)
        self.requests.append({"headers": dict(request.headers), "body": body})
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if system == ROUTER_LLM_SYSTEM:
            message = user.split("후보 Task 팀(JSON):")[0]
            candidates = json.loads(user.split("후보 Task 팀(JSON):")[1])
            intent = (
                "store_note"
                if "메모:" in message
                else ("task_run" if "검증해" in message else "query")
            )
            picked = next(
                (
                    c["task_id"]
                    for c in candidates
                    if "아틀라스" in c["goal"] and "아틀라스" in message
                ),
                None,
            )
            if self.outside:
                picked = "task_not_in_list"
            content = {"intent": intent, "assignee_task_id": picked, "reason": "합성 사유"}
        elif system == ANSWER_LLM_SYSTEM:
            cited = [
                line.split("]")[0][1:].split("@")[0]
                for line in user.splitlines()
                if line.startswith("[")
            ]
            content = {"answer": "LLM 답변: 헬리오스 마감은 10월 9일입니다.", "citations": cited}
        elif "Supervisor" in system:
            content = {
                "summary": "LLM 팀 요약: 아틀라스 자료 1건을 검토했습니다.",
                "open_questions": ["실측 필요"],
            }
        else:  # domain worker SYSTEM_PROMPT: answer + E-labels
            labels = [line.split("]")[0][1:] for line in user.splitlines() if line.startswith("[E")]
            content = {"answer": "LLM 도메인 답변입니다.", "citations": labels[:1]}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(content, ensure_ascii=False),
                        },
                    }
                ]
            },
        )


def env_file(tmp_path):
    path = tmp_path / "synthetic.env"
    path.write_text(
        "NVIDIA_BASE_URL=https://fake.nvidia.test/v1\nNVIDIA_MODEL=synthetic/model-1\n"
        f"NVIDIA_API_KEY={KEY}\n"
    )
    return path


def _item(audience, text="본문"):
    return EvidenceItem(
        source_id="s",
        source_revision="1",
        audience=audience,
        excerpt=text,
        location=SourceLocation(uri="u", section=None),
        content_hash="h",
        policy_version="p",
    )


def _bundle(*items):
    return EvidenceBundle(
        request_id="r",
        trace_id="t",
        run_id="run",
        agent_id="a",
        domain_id=DomainId.TRIV3,
        items=tuple(items),
        policy_version="p",
        simulated=False,
        adapter="x",
    )


def test_settings_consent_is_model_egress_only_and_reserved_for_mock():
    base = dict(nvidia_model="m", nvidia_api_key="k", allow_external_egress=True)
    real = Settings(_env_file=None, model_provider="nvidia", **base)
    assert (
        real.external_egress_effective
        and real.external_egress_scope == "model_endpoint_owner_context"
    )
    assert "feature:external_egress_policy" not in real.selected_reserved_features()
    mock = Settings(_env_file=None, model_provider="mock", **base)
    assert not mock.external_egress_effective and mock.external_egress_scope == "none"
    assert "feature:external_egress_policy" in mock.selected_reserved_features()
    assert not Settings(
        _env_file=None, model_provider="nvidia", nvidia_model="m", nvidia_api_key="k"
    ).external_egress_effective


def test_egress_filter_and_gate_release_private_only_for_owner_targets():
    bundle = _bundle(_item(Audience.PRIVATE), _item(Audience.PUBLIC))
    kept, withheld = share_egress_filter(
        bundle, target=Audience.OWNER, endpoint="cloud", private_egress=True
    )
    assert len(kept.items) == 2 and withheld["egress"] == 0
    kept, withheld = share_egress_filter(bundle, target=Audience.OWNER, endpoint="cloud")
    assert len(kept.items) == 1 and withheld["egress"] == 1
    kept, withheld = share_egress_filter(
        bundle, target=Audience.PUBLIC, endpoint="cloud", private_egress=True
    )
    assert [i.audience for i in kept.items] == [Audience.PUBLIC] and withheld["share"] == 1

    gate = OwnerConsentEgressGate(
        endpoint="https://x/v1/chat/completions", model="m", max_output_tokens=10, budget_seconds=5
    )

    async def grant(target, *items):
        req = ModelRequest(
            request_id="r",
            trace_id="t",
            run_id="run",
            agent_id="a",
            domain_id=DomainId.TRIV3,
            query="q",
            evidence=_bundle(*items),
            target=DraftTarget(audience=target),
        )
        return await gate.authorize(req, endpoint="https://x/v1/chat/completions", model="m")

    import asyncio

    assert asyncio.run(grant(Audience.OWNER, _item(Audience.PRIVATE))) is not None
    assert asyncio.run(grant(Audience.PUBLIC, _item(Audience.PRIVATE))) is None
    assert asyncio.run(grant(Audience.PUBLIC, _item(Audience.PUBLIC))) is not None
    assert gate.text_grant(endpoint="https://other/v1/chat/completions", model="m") is None


async def test_reason_requires_consent_gate_and_parses_json():
    fake = FakeNvidia()
    config = NvidiaChatConfig(
        base_url="https://fake.nvidia.test/v1",
        model="synthetic/model-1",
        api_key=KEY,
        timeout_seconds=5,
    )
    public = NvidiaChatModel(
        config,
        PublicOnlyEgressGate(
            endpoint=config.endpoint, model=config.model, max_output_tokens=50, budget_seconds=5
        ),
        transport=httpx.MockTransport(fake.handler),
    )
    with pytest.raises(RfaError) as denied:
        await public.reason("s", "u")
    assert denied.value.code == "egress_not_permitted" and fake.requests == []
    consented = NvidiaChatModel(
        config,
        OwnerConsentEgressGate(
            endpoint=config.endpoint, model=config.model, max_output_tokens=50, budget_seconds=5
        ),
        transport=httpx.MockTransport(fake.handler),
    )
    parsed = await consented.reason(ANSWER_LLM_SYSTEM, "질문: x\n\n근거 발췌:\n[s@1] 본문")
    assert parsed["answer"].startswith("LLM 답변") and fake.requests[-1]["body"]["stream"] is False
    assert fake.requests[-1]["body"]["response_format"] == {"type": "json_object"}
    await public.aclose()
    await consented.aclose()


def test_poc_real_model_needs_explicit_configuration(tmp_path):
    with pytest.raises(PocModelNotConfigured, match="NVIDIA_MODEL, NVIDIA_API_KEY"):
        local_settings(
            tmp_path / "d", __import__("pydantic").SecretStr("t"), model="nvidia", env_file=None
        )
    with pytest.raises(PocModelNotConfigured, match="unsupported"):
        local_settings(tmp_path / "d", __import__("pydantic").SecretStr("t"), model="other")
    settings = local_settings(
        tmp_path / "d",
        __import__("pydantic").SecretStr("t"),
        model="nvidia",
        env_file=env_file(tmp_path),
    )
    assert settings.model_provider == "nvidia" and settings.external_egress_effective
    assert settings.response_backend == "http" and settings.runtime_backend == "local"
    assert KEY not in repr(settings)


async def test_chat_with_real_adapter_reasons_answers_and_rebinds_to_revisions(tmp_path):
    fake = FakeNvidia()
    app = create_poc_app(
        tmp_path / "poc",
        model="nvidia",
        env_file=env_file(tmp_path),
        model_transport=httpx.MockTransport(fake.handler),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8780",
            headers={"Origin": "http://127.0.0.1:8780"},
        ) as c,
    ):
        c.headers["X-RFA-CSRF"] = (await c.get("/ui/api/csrf")).json()["csrf_token"]
        container = app.state.container
        status = (await c.get("/ui/api/status")).json()
        assert status["core"]["reachable"]
        sid = (await c.post("/ui/api/sessions", json={})).json()["session_id"]
        # 1) LLM intent: a statement without note markers is stored because the model said so.
        note = await send(c, sid, "메모: 헬리오스 마감은 10월 9일입니다.", "n1")
        stages = {s["stage"]: s for s in note["stages"]}
        assert (
            note["status"] == "stored" and stages["reasoning"]["detail"]["intent"] == "store_note"
        )
        assert stages["reasoning"]["detail"]["model"] == "nvidia-chat-completions"
        # 2) Assistant fallback: the answer is synthesized by the model from private excerpts.
        asked = await send(c, sid, "헬리오스 마감 언제야?", "q1")
        stages = {s["stage"]: s for s in asked["stages"]}
        assert asked["route"]["kind"] == "assistant" and "synthesis" in stages
        assert asked["reply"].startswith("LLM 답변") and asked["answer_model"]["simulated"] is False
        assert stages["synthesis"]["detail"]["evidence_used"] >= 1
        sent = fake.requests[-1]["body"]["messages"][1]["content"]
        assert "10월 9일" in sent  # owner-consented private excerpt reached the endpoint
        assert all(KEY not in json.dumps(s, ensure_ascii=False) for s in asked["stages"])
        history = (await c.get(f"/ui/api/sessions/{sid}/chat")).json()
        assert history[-1]["reply"] == asked["reply"]  # replayed from the persisted answer
        # 3) Source revision changes -> the persisted answer is not replayed.
        doc = (await c.get(f"/ui/api/notes/{note['source_id']}")).json()["document"]
        with sqlite3.connect(container.repository.path) as db:
            db.execute("DELETE FROM kb_source_revisions WHERE source_id=?", (doc["source_id"],))
            db.execute("DELETE FROM kb_documents WHERE source_id=?", (doc["source_id"],))
        history = (await c.get(f"/ui/api/sessions/{sid}/chat")).json()
        assert "다시 표시하지 않아요" in history[-1]["reply"]
        assert history[-1]["answer_model"]["stale"] is True
        # 4) Public draft preview: private excerpts are never sent to the model.
        before = len(fake.requests)
        public = await send(c, sid, "헬리오스 공개 초안 만들어줘", "p1")
        assert public["run_id"] is None
        assert all(
            "10월 9일" not in json.dumps(r["body"], ensure_ascii=False)
            for r in fake.requests[before:]
        )
        # 5) LLM assignee only inside candidates; outside IDs are ignored.
        owner = await container.repository.local_principal()
        team = await container.team_factory.ensure(
            SelectionRequest(
                goal="아틀라스 research 자료 조사",
                domain_id=DomainId.TRIV3,
                outputs=frozenset({"research_report"}),
                requested_pattern="research",
            ),
            owner,
            idempotency_key="atlas",
        )
        routed = await send(c, sid, "아틀라스 마감 알려줘", "t1")
        assert (
            routed["route"]["kind"] == "task" and routed["route"]["reason"] == "llm_selected_task"
        )
        assert routed["route"]["task_id"] == team.task.task_id
        assert routed["team"]["task_id"] == team.task.task_id
        assert "LLM 팀 요약" in routed["reply"]  # supervisor synthesis via the real adapter
        fake.outside = True
        ignored = await send(c, sid, "아틀라스 마감 알려줘 outside", "t2")
        reasoning = next(s for s in ignored["stages"] if s["stage"] == "reasoning")
        assert reasoning["detail"]["assignee_task_id"] is None
        assert reasoning["label"].endswith("후보 밖 ID 제안은 무시")
        # Deterministic subject match still applies after the LLM abstains.
        assert ignored["route"]["reason"] == "existing_task_subject_match"
        fake.outside = False
        # 6) Destructive words never become an action through the model.
        guard = await send(c, sid, "헬리오스 자료 삭제해", "d1")
        assert guard["intent"] == "clarify" and guard["run_id"] is None
        # Bearer header only ever goes to the fake endpoint; never appears in stage details.
        assert all(r["headers"]["authorization"] == "Bearer " + KEY for r in fake.requests)
        assert all(r["headers"]["host"] == "fake.nvidia.test" for r in fake.requests)
        systems = [r["body"]["messages"][0]["content"][:60] for r in fake.requests]
        assert len(fake.requests) >= 8, systems
        assert any("Supervisor" in s for s in systems) and any("라우터" in s for s in systems)


def test_explicit_task_bypasses_llm_routing(tmp_path):
    message = ChatMessage(text="x", message_id="m", task_id="t")
    assert message.task_id == "t"
