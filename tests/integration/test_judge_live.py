"""Opt-in live smoke: NVIDIA Nemotron Judge over one synthetic public Run (P1-006A).

One mock-model Run over the seeded synthetic fixtures (public audience) is scored by the
hosted Judge model from the explicit env file. The score is auxiliary: it says nothing
about privacy or access, and a simulated persona is not a satisfied user.

Run explicitly (skip is not success):
    RFA_JUDGE_LIVE=1 RFA_NVIDIA_ENV_FILE=/abs/.env.dev \
    RFA_JUDGE_EVIDENCE_OUT=/abs/evidence.json \
    .venv/bin/python -m pytest -q tests/integration/test_judge_live.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from rfa_mas.adapters.nvidia import NvidiaChatConfig
from rfa_mas.adapters.nvidia_judge import (
    PROMPT_VERSION,
    RUBRIC_VERSION,
    NvidiaJudge,
    SyntheticPublicJudgeGate,
)
from rfa_mas.application.evaluation import evaluate_case
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftTarget,
    EvaluationCaseV11,
    WorkRequest,
    WorkStatus,
)
from rfa_mas.settings import Settings

LIVE_ENABLED = os.environ.get("RFA_JUDGE_LIVE") == "1"
ENV_FILE = os.environ.get("RFA_NVIDIA_ENV_FILE")
EVIDENCE_OUT = os.environ.get("RFA_JUDGE_EVIDENCE_OUT")
TIMEOUT_SECONDS = 170.0
BUDGET_SECONDS = 360.0
QUESTION = "TRIV3의 공개 트랙을 근거와 함께 요약해 줘."
CANARY_PREFIX = "SYNTHETIC_PRIVATE_CANARY_"
_FAILURE: dict[str, str] = {}

pytestmark = pytest.mark.skipif(
    not (LIVE_ENABLED and ENV_FILE),
    reason="opt-in live NVIDIA Judge smoke; skipped run is not evidence",
)


def _case() -> EvaluationCaseV11:
    return EvaluationCaseV11.model_validate(
        {
            "case_id": "judge-live-01",
            "persona": "owner",
            "input": QUESTION,
            "material_scope": {
                "domain_id": "triv3",
                "authenticated_principal": "fixture-owner-001",
                "memberships": ["company", "triv3-team"],
                "requested_audience": "public",
                "allowed_source_ids": ["triv3-public-overview"],
            },
            "expected_evidence": ["triv3-public-overview"],
            "forbidden_information": [CANARY_PREFIX + "TRIV3_7Q9X_DO_NOT_DISCLOSE"],
            "expected_behavior": "answer_with_public_evidence",
            "scenario_id": "judge-live-01",
            "identity_fixture_id": "owner",
            "fixture_ref": "judge-live-v1",
            "dataset_version": "judge-live-v1",
            "seed": 1,
            "expected_observations": ["identity"],
        }
    )


class LiveRun:
    def __init__(self, env_file: Path, workdir: Path) -> None:
        settings = Settings(_env_file=env_file)
        secret = settings.nvidia_api_key
        if secret is None or not secret.get_secret_value():
            pytest.fail("NVIDIA_API_KEY missing in the explicit env file")
        model = settings.judge_model or settings.nvidia_model
        if not model:
            pytest.fail("JUDGE_MODEL (or NVIDIA_MODEL) missing in the explicit env file")
        self._secret = secret
        self.config = NvidiaChatConfig(
            base_url=settings.nvidia_base_url,
            model=model,
            api_key=SecretStr(secret.get_secret_value()),
            timeout_seconds=TIMEOUT_SECONDS,
            max_attempts=2,
        )
        self.workdir = workdir
        self.results: dict[str, Any] = {"model": model, "endpoint": self.config.endpoint}

    def leaks(self, text: str) -> bool:
        return self._secret.get_secret_value() in text

    async def run(self) -> None:
        container = build_container(
            Settings(
                _env_file=None,
                database_url=f"sqlite:///{self.workdir / 'judge.db'}",
                trace_dir=(self.workdir / "traces").resolve(),
            )
        )
        await container.startup()
        judge = NvidiaJudge(
            self.config,
            SyntheticPublicJudgeGate(
                endpoint=self.config.endpoint,
                model=self.config.model,
                max_output_tokens=512,
                budget_seconds=BUDGET_SECONDS,
                max_attempts=2,
            ),
        )
        try:
            owner = await container.repository.local_principal()
            result = await container.service.run(
                WorkRequest(
                    query=QUESTION,
                    domain_id=DomainId.TRIV3,
                    target=DraftTarget(audience=Audience.PUBLIC),
                ),
                owner,
            )
            self.results["run_status"] = result.status.value
            if result.status != WorkStatus.COMPLETED or result.draft is None:
                pytest.fail(f"mock run did not complete: {[e.code for e in result.errors]}")
            self.results["draft_sha256"] = hashlib.sha256(result.draft.content.encode()).hexdigest()
            self.results["draft_evidence"] = [e.source_id for e in result.draft.allowed_evidence]
            started = time.monotonic()
            evaluation = await evaluate_case(judge, _case(), result)
            self.results["judge_seconds"] = round(time.monotonic() - started, 2)
            self.results["evaluation"] = evaluation.model_dump(mode="json")
        finally:
            await judge.aclose()
            await container.shutdown()


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> LiveRun:
    if "reason" in _FAILURE:
        pytest.fail(_FAILURE["reason"])
    env_file = Path(ENV_FILE or "")
    if env_file.is_symlink() or not env_file.is_file():
        pytest.fail("RFA_NVIDIA_ENV_FILE must be an explicit regular file")
    run = LiveRun(env_file, tmp_path_factory.mktemp("judge-live"))
    try:
        asyncio.run(run.run())
    except BaseException as exc:
        _FAILURE["reason"] = f"live judge run failed once ({type(exc).__name__}); not retried"
        raise
    _write_evidence(run)
    return run


def test_actual_judge_assessment_is_recorded_beside_rules(live: LiveRun) -> None:
    ev = live.results["evaluation"]
    assert ev["judge_kind"] == "actual" and ev["executed"] is True
    assert ev["simulated"] is False and ev["judge_adapter"] == "nvidia-judge"
    dims = ev["judge_dimensions"]
    assert all(
        0.0 <= dims[k] <= 1.0
        for k in ("evidence_faithfulness", "question_resolution", "task_candidate_usefulness")
    )
    assert 0.0 <= ev["judge_score"] <= 1.0
    assert RUBRIC_VERSION in ev["judge_reason"] and PROMPT_VERSION in ev["judge_reason"]
    assert ev["rule_checks"]  # deterministic gates recorded independently of the score


def test_no_credential_or_private_marker_in_results(live: LiveRun) -> None:
    dumped = json.dumps(live.results, ensure_ascii=False)
    assert not live.leaks(dumped) and CANARY_PREFIX not in dumped
    if EVIDENCE_OUT:
        assert not live.leaks(Path(EVIDENCE_OUT).read_text(encoding="utf-8"))


def _write_evidence(run: LiveRun) -> None:
    if not EVIDENCE_OUT:
        return
    document = {
        "task": "P1-006A",
        "kind": "live_judge_smoke",
        "simulated": False,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rubric_version": RUBRIC_VERSION,
        "prompt_version": PROMPT_VERSION,
        "note": "auxiliary quality score over one synthetic public mock Run; not a privacy, "
        "access or user-satisfaction measurement; n=1",
        **run.results,
    }
    text = json.dumps(document, ensure_ascii=False, indent=2)
    if run.leaks(text):
        pytest.fail("evidence would contain the credential; not written")
    Path(EVIDENCE_OUT).write_text(text + "\n", encoding="utf-8")
