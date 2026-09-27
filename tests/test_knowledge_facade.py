"""P1-010: rfa_mas provides RFA_module's knowledge contract (실무대장 facade).

Offline: synthetic fixtures, mock model, tmp_path SQLite. Compatibility is asserted against
the pinned copy of 승희's contract, not against a live RFA_module service.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from helpers.openapi_check import OpenApiDocument
from pydantic import SecretStr

from rfa_mas.contracts import Audience, DomainId, KnowledgeWrite
from rfa_mas.knowledge_facade.app import create_knowledge_facade_app
from rfa_mas.knowledge_facade.service import (
    KnowledgeFacadeService,
    lexical_confidence,
    source_line,
)

ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / "fixtures" / "contracts" / "rfa_module" / "knowledge.openapi.yaml"
CANARY_TITLE = "TRIV3 소유자 전용 합성 privacy canary"


@pytest.fixture
def contract() -> OpenApiDocument:
    return OpenApiDocument(PINNED)


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://facade")


async def test_list_tasks_and_ask_match_pinned_contract(container, contract):
    app = create_knowledge_facade_app(container)
    async with app.router.lifespan_context(app), await _client(app) as client:
        listed = await client.get("/tasks", headers={"X-RFA-Actor": "knowledge"})
        assert listed.status_code == 200
        tasks = listed.json()
        assert contract.validate(contract.response_schema("/tasks", "get", "200"), tasks) == []
        assert {task["id"] for task in tasks} == {"triv3", "quantization_research"}
        assert all(task["name"] and task["description"] for task in tasks)

        asked = await client.post(
            "/tasks/triv3/ask",
            json={"question": "TRIV3 벤치마크 트랙 진행 상황을 알려줘", "hint": "ignored"},
        )
        assert asked.status_code == 200
        result = asked.json()
        schema = contract.response_schema("/tasks/{task_id}/ask", "post", "200")
        assert contract.validate(schema, result) == []
        assert result["task_id"] == "triv3"
        assert result["answer"].strip()
        assert 0.0 < result["confidence"] <= 1.0
        assert result["sources"] and all(": " in line for line in result["sources"])

        missing = await client.post("/tasks/orbit/ask", json={"question": "ORBIT 진행?"})
        assert missing.status_code == 404
        assert missing.json() == {"error": "not_found", "id": "orbit"}

        invalid = await client.post("/tasks/triv3/ask", json={"question": ""})
        assert invalid.status_code == 422


async def test_public_facade_serves_public_evidence_only(container, principal):
    # An owner-private note that mentions the public topic must never reach the writer.
    await container.knowledge.write(
        KnowledgeWrite(
            domain_id=DomainId.TRIV3,
            title="TRIV3 개인 메모",
            content=(
                "TRIV3 벤치마크 트랙 비공개 메모 SYNTHETIC_PRIVATE_CANARY_FACADE_001 출시일 미정"
            ),
            provenance={"provider": "note", "namespace": "notes", "external_id": "facade-1"},
            provider_revision="1",
        ),
        principal,
    )
    app = create_knowledge_facade_app(container)
    async with app.router.lifespan_context(app), await _client(app) as client:
        response = await client.post(
            "/tasks/triv3/ask", json={"question": "TRIV3 벤치마크 트랙 진행과 출시일"}
        )
        payload = json.dumps(response.json(), ensure_ascii=False)
        assert response.status_code == 200
        assert "CANARY" not in payload
        assert "비공개 메모" not in payload and "사내" not in payload
        assert "팀 내부" not in payload
        assert CANARY_TITLE not in payload
        for line in response.json()["sources"]:
            assert line.startswith("TRIV3 합성 벤치마크 공개 개요")

        # A question that already carries a private marker gets nothing back.
        leaked = await client.post(
            "/tasks/triv3/ask",
            json={"question": "SYNTHETIC_PRIVATE_CANARY_FACADE_001 내용이 뭐야"},
        )
        assert leaked.status_code == 200
        assert leaked.json() == {
            "task_id": "triv3",
            "answer": "",
            "confidence": 0.0,
            "sources": [],
        }


async def test_no_evidence_returns_empty_answer_for_reselection(container):
    # RFA_module's ask_knowledge treats answer.strip()=="" as "no knowledge" and re-picks.
    app = create_knowledge_facade_app(container)
    async with app.router.lifespan_context(app), await _client(app) as client:
        response = await client.post("/tasks/triv3/ask", json={"question": "zqxv wpl"})
        assert response.status_code == 200
        assert response.json()["answer"] == ""
        assert response.json()["sources"] == []
        assert response.json()["confidence"] == 0.0


async def test_non_public_audience_requires_bearer_key(container):
    with pytest.raises(ValueError):
        create_knowledge_facade_app(container, audience=Audience.COMPANY)
    with pytest.raises(ValueError):
        KnowledgeFacadeService.from_container(container, audience=Audience.OWNER)
    app = create_knowledge_facade_app(
        container, audience=Audience.COMPANY, api_key=SecretStr("facade-test-key")
    )
    async with app.router.lifespan_context(app), await _client(app) as client:
        denied = await client.get("/tasks")
        assert denied.status_code == 401
        assert denied.json() == {"error": "authentication_required"}
        wrong = await client.get("/tasks", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        allowed = await client.get("/tasks", headers={"Authorization": "Bearer facade-test-key"})
        assert allowed.status_code == 200
        asked = await client.post(
            "/tasks/triv3/ask",
            json={"question": "TRIV3 사내 비교 가이드"},
            headers={"Authorization": "Bearer facade-test-key"},
        )
        assert asked.status_code == 200
        joined = " ".join(asked.json()["sources"])
        # Company audience may see company material but still no owner/business-unit items.
        assert CANARY_TITLE not in joined and "팀 내부" not in joined


def test_confidence_and_source_line_helpers():
    assert lexical_confidence("TRIV3 트랙 진행", ("TRIV3 트랙은 세 개",)) == 0.67
    assert lexical_confidence("", ("x",)) == 0.0
    assert lexical_confidence("질문", ()) == 0.0
    assert source_line("제목", "\n첫 줄\n둘째 줄") == "제목: 첫 줄"
    assert source_line(None, "x" * 200).endswith("…")


def test_cli_rejects_non_public_audience_without_key(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(ROOT / "src")}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rfa_mas.cli",
            "knowledge-facade",
            "--audience",
            "company",
            "--port",
            "1",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout.strip().splitlines()[-1])["code"] == "configuration_error"


def test_openapi_export_matches_pinned_operations(container, contract):
    document = create_knowledge_facade_app(container).openapi()
    ours = {
        (path, method): op.get("operationId")
        for path, methods in document["paths"].items()
        for method, op in methods.items()
        if path != "/healthz"
    }
    theirs = {
        (path, method): op.get("operationId")
        for path, methods in contract.document["paths"].items()
        for method, op in methods.items()
    }
    assert ours == theirs
