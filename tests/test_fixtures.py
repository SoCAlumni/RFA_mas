from __future__ import annotations

import json
import re
from collections import Counter
from itertools import product
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from rfa_mas.contracts import Audience, DomainId, EvaluationCase, KnowledgeDocument

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_DIR = PROJECT_ROOT / "fixtures" / "documents"
EVALUATION_CASES = PROJECT_ROOT / "fixtures" / "eval" / "evaluation_cases.jsonl"

PERSONAS = {
    "owner",
    "business_unit_colleague",
    "company_other_unit",
    "external",
}
SITUATIONS = {f"{number:02d}" for number in range(1, 7)}


def _jsonl_records(path: Path) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            records.append((line_number, json.loads(line)))
    return records


def _validate_records(
    path: Path,
    model_type: type[KnowledgeDocument] | type[EvaluationCase],
) -> list[KnowledgeDocument] | list[EvaluationCase]:
    validated: list[KnowledgeDocument] | list[EvaluationCase] = []
    errors: list[str] = []
    for line_number, payload in _jsonl_records(path):
        try:
            validated.append(model_type.model_validate(payload))
        except ValidationError as exc:
            errors.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number}: {exc}")
    assert not errors, "Invalid JSONL fixture records:\n" + "\n".join(errors)
    return validated


def test_document_fixtures_are_valid_synthetic_contracts() -> None:
    paths = sorted(DOCUMENT_DIR.glob("*.jsonl"))
    expected_domain = {
        "quantization_research.jsonl": DomainId.QUANTIZATION_RESEARCH,
        "triv3.jsonl": DomainId.TRIV3,
    }
    assert {path.name for path in paths} == set(expected_domain)

    documents: list[KnowledgeDocument] = []
    keys: set[tuple[str, str]] = set()
    by_domain: dict[DomainId, list[KnowledgeDocument]] = {
        domain_id: [] for domain_id in expected_domain.values()
    }
    for path in paths:
        validated = _validate_records(path, KnowledgeDocument)
        assert all(isinstance(item, KnowledgeDocument) for item in validated)
        for item in validated:
            assert isinstance(item, KnowledgeDocument)
            assert item.domain_id == expected_domain[path.name]
            assert item.location.uri == f"fixture://documents/{path.name}"
            assert item.synthetic is True
            assert (item.source_id, item.source_revision) not in keys
            keys.add((item.source_id, item.source_revision))
            documents.append(item)
            by_domain[item.domain_id].append(item)

    assert documents
    for domain_documents in by_domain.values():
        assert {item.audience for item in domain_documents}.issuperset(
            {
                Audience.PUBLIC,
                Audience.BUSINESS_UNIT,
                Audience.COMPANY,
                Audience.OWNER,
            }
        )
        canaries = [item for item in domain_documents if item.privacy_canary]
        assert len(canaries) == 1
        assert canaries[0].audience == Audience.OWNER
        assert "SYNTHETIC_PRIVATE_CANARY_" in canaries[0].content


def test_evaluation_fixture_is_exactly_four_personas_by_six_situations() -> None:
    raw_records = _jsonl_records(EVALUATION_CASES)
    assert len(raw_records) == 24

    validated = _validate_records(EVALUATION_CASES, EvaluationCase)
    assert all(isinstance(item, EvaluationCase) for item in validated)
    cases = [item for item in validated if isinstance(item, EvaluationCase)]

    assert len(cases) == 24
    assert len({case.case_id for case in cases}) == 24
    assert Counter(case.persona for case in cases) == Counter({persona: 6 for persona in PERSONAS})

    observed_matrix: set[tuple[str, str]] = set()
    for case in cases:
        match = re.search(r"-(0[1-6])-", case.case_id)
        assert match is not None, f"case_id has no 01..06 situation: {case.case_id}"
        situation = match.group(1)
        observed_matrix.add((case.persona, situation))
        assert set(case.expected_evidence).issubset(case.material_scope.allowed_source_ids)

    assert observed_matrix == set(product(PERSONAS, SITUATIONS))


def test_evaluation_source_references_exist_in_document_fixtures() -> None:
    source_ids = {
        payload["source_id"]
        for path in DOCUMENT_DIR.glob("*.jsonl")
        for _, payload in _jsonl_records(path)
    }
    cases = _validate_records(EVALUATION_CASES, EvaluationCase)

    for case in cases:
        assert isinstance(case, EvaluationCase)
        assert set(case.material_scope.allowed_source_ids).issubset(source_ids)
        assert set(case.expected_evidence).issubset(source_ids)
