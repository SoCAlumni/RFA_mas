from __future__ import annotations

import hashlib
import uuid
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"
SchemaVersion = Literal["1.0"]

# The 1.0 wire contract remains frozen. New DTOs explicitly opt into 1.1.
EXTENDED_SCHEMA_VERSION = "1.1"
OpaqueId = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ExecutionMode(StrEnum):
    MOCK = "mock"
    LOCAL = "local"
    REAL = "real"


class ContextLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class EvaluationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    NOT_RUN = "not_run"
    UNKNOWN = "unknown"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: SchemaVersion = SCHEMA_VERSION


class ExtendedContractModel(ContractModel):
    """Explicit 1.1 opt-in; do not strip the bytes to which approval is bound."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, allow_inf_nan=False)
    schema_version: Literal["1.1"] = EXTENDED_SCHEMA_VERSION


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
