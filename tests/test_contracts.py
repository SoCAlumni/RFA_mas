import pytest
from pydantic import ValidationError

from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
    sha256_text,
)


def _draft_payload() -> dict[str, object]:
    content = "synthetic draft"
    return {
        "request_id": "req-contract",
        "trace_id": "trace-contract",
        "run_id": "run-contract",
        "agent_id": "domain-supervisor:triv3",
        "domain_id": DomainId.TRIV3,
        "draft_id": "draft-contract",
        "version": 1,
        "content_hash": sha256_text(content),
        "target": DraftTarget(audience=Audience.PUBLIC),
        "audience": Audience.PUBLIC,
        "policy_version": "local-v1",
        "allowed_evidence": (),
        "content": content,
        "simulated": True,
        "adapter": "test",
    }


def test_draft_content_hash_is_bound_to_exact_content() -> None:
    payload = _draft_payload()
    payload["content_hash"] = sha256_text("different content")

    with pytest.raises(ValidationError, match="content_hash"):
        DraftBundle.model_validate(payload)


def test_draft_audience_is_bound_to_target() -> None:
    payload = _draft_payload()
    payload["audience"] = Audience.COMPANY

    with pytest.raises(ValidationError, match="audience"):
        DraftBundle.model_validate(payload)
