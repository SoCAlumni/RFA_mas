"""RFA_module knowledge contract (실무대장 ↔ 언론사) as provider-side Pydantic models.

The contract is owned by 승희's RFA_module (`contracts/knowledge.openapi.yaml`, pinned copy
under `fixtures/contracts/rfa_module/`). Field names and constraints stay 1:1 with
`rfa_common.models`; this module never redefines the shared RFA 1.0/1.1 DTOs.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

KNOWLEDGE_CONTRACT_VERSION = "0.1.0"
KNOWLEDGE_CONTRACT_SOURCE = "SoCAlumni/RFA_module@819053f contracts/knowledge.openapi.yaml"


class KnowledgeContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskInfo(KnowledgeContractModel):
    """One live task the writer may ask about (`GET /tasks`)."""

    id: str = Field(min_length=1, max_length=120, examples=["triv3"])
    name: str
    description: str
    updated_at: date


class AskRequest(BaseModel):
    """`POST /tasks/{task_id}/ask` body. Extra hint fields from newer callers are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    question: str = Field(min_length=1, max_length=10_000)


class KnowledgeResult(KnowledgeContractModel):
    """Answer plus one evidence line per source. Empty `answer` means "no knowledge"."""

    task_id: str
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[str] = Field(
        default_factory=list, description="answer 의 근거를 한 줄씩. 없으면 []"
    )


class KnowledgeNotFound(KnowledgeContractModel):
    error: Literal["not_found"] = "not_found"
    id: str


class KnowledgeUnauthorized(KnowledgeContractModel):
    error: Literal["authentication_required"] = "authentication_required"
