from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from rfa_mas.contracts import ToolResult
from rfa_mas.reference.app import create_reference_contract_app
from scripts.contract_baseline import (
    BASELINE,
    ReferenceCase,
    build_baseline,
    digest,
    execute_case,
    load_cases,
    offline_settings,
    project_response,
)


def test_export_matches_recorded_baseline_and_digest() -> None:
    recorded = json.loads(BASELINE.read_text(encoding="utf-8"))
    generated = build_baseline()
    assert recorded == generated
    payload = {key: value for key, value in recorded.items() if key != "digest"}
    assert recorded["digest"] == digest(payload)
    assert recorded["status"] == "repository-local-provisional"
    assert not {"Session", "TeamSpec", "TeamInstance"}.intersection(recorded["public_model_names"])
    assert recorded["ports"]["RuntimePort"].keys() == {"run", "status", "cancel"}


def test_all_schema_and_openapi_references_resolve() -> None:
    baseline = build_baseline()

    def walk(value: Any, document: dict[str, Any]) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/")
                target: Any = document
                for segment in value["$ref"][2:].split("/"):
                    target = target[segment.replace("~1", "/").replace("~0", "~")]
                assert isinstance(target, dict)
            for child in value.values():
                walk(child, document)
        elif isinstance(value, list):
            for child in value:
                walk(child, document)

    for schema in [baseline["json_schema"], *baseline["openapi"].values()]:
        walk(schema, schema)
    assert "/v1/work" in baseline["openapi"]["core"]["paths"]
    assert "/v1/reviews" in baseline["openapi"]["teammate_reference"]["paths"]


def test_offline_settings_ignore_process_configuration(monkeypatch, tmp_path: Path) -> None:
    # Synthetic environment trap: the exporter must not select or consume real providers.
    monkeypatch.setenv("MODEL_PROVIDER", "nvidia")
    monkeypatch.setenv("NVIDIA_API_KEY", "SYNTHETIC_ENV_TRAP_NOT_A_CREDENTIAL")
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    settings = offline_settings(tmp_path)
    assert settings.model_provider == "mock"
    assert settings.nvidia_api_key is None
    assert settings.app_host == "127.0.0.1"
    assert settings.database_path == tmp_path / "contract.db"


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case.id)
async def test_reference_cases_match_actual_local_or_mock_behavior(case, tmp_path: Path) -> None:
    actual = await execute_case(case, tmp_path)
    assert project_response(actual) == case.assertions
    assert "SYNTHETIC_PRIVATE_CANARY" not in actual.model_dump_json()
    if case.id == "work-permission_denied":
        assert case.principal.authenticated is False
        assert case.request["simulation_scenario"] == "success"
    if case.category == "partial_failure":
        assert actual.draft is not None
        assert actual.review is None
        assert actual.publication_status == "not_requested"
    if case.kind == "tool":
        # Same request/response DTO and observable behavior through the provisional HTTP surface.
        app = create_reference_contract_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post("/v1/tools/execute", json=case.request)
        assert response.status_code == 200
        assert ToolResult.model_validate(response.json()) == actual


def test_fixture_schema_rejects_unknown_fields_and_unbound_response() -> None:
    case = load_cases()[0].model_dump(mode="json")
    changed = copy.deepcopy(case)
    changed["request"]["unverified_identity_claim"] = "owner"
    with pytest.raises(ValidationError):
        ReferenceCase.model_validate(changed)
    changed = copy.deepcopy(case)
    changed["response"]["run_id"] = "different-run"
    with pytest.raises(ValidationError, match="binding mismatch"):
        ReferenceCase.model_validate(changed)
    changed = copy.deepcopy(case)
    changed["assertions"]["status"] = "outcome_unknown"
    with pytest.raises(ValidationError, match="declared assertions"):
        ReferenceCase.model_validate(changed)
