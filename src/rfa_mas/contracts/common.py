from __future__ import annotations

import hashlib
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"
SchemaVersion = Literal["1.0"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: SchemaVersion = SCHEMA_VERSION


class Audience(StrEnum):
    PRIVATE = "private"
    OWNER = "owner"
    BUSINESS_UNIT = "business_unit"
    COMPANY = "company"
    PUBLIC = "public"


class DomainId(StrEnum):
    TRIV3 = "triv3"
    QUANTIZATION_RESEARCH = "quantization_research"


class WorkStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REVISION_REQUESTED = "revision_requested"
    REJECTED = "rejected"


class PublicationStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"


class FeedbackCategory(StrEnum):
    STYLE_PREFERENCE = "style_preference"
    FACTUAL_CORRECTION = "factual_correction"
    PERSONAL_DISCLOSURE_PREFERENCE = "personal_disclosure_preference"
    OFFICIAL_POLICY_CHANGE_PROPOSAL = "official_policy_change_proposal"


class SimulationScenario(StrEnum):
    SUCCESS = "success"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    POLICY_DENIED = "policy_denied"
    REVISION_REQUESTED = "revision_requested"
    TIMEOUT = "timeout"


class AdapterInfo(ContractModel):
    port: str
    adapter: str
    simulated: bool


class StructuredError(ContractModel):
    code: str
    retryable: bool = False
    message: str
    request_id: str | None = None
    trace_id: str | None = None
    run_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
