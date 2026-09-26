"""Pure, fail-closed selection from server-approved team definitions.

Composition supplies the registry, authenticated principal, subject/domain grants,
available runtime capabilities and budget ceiling. None is accepted from goal
text, a retrieved document, or an LLM. Selection does not provision a team or
authorize individual tool/data operations: TeamFactory and PolicyPort do that.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rfa_mas.contracts import DomainId, TeamBudget, TeamTemplate, TrustedPrincipal
from rfa_mas.errors import RfaError

BUDGET_FIELDS = ("max_steps", "max_tool_calls", "max_tokens", "timeout_seconds", "concurrency")
PATTERN_ROLES = MappingProxyType(
    {
        "benchmark": ("supervisor", "paper_scout", "experiment_runner", "result_analyst"),
        "research": ("supervisor", "source_scout", "evidence_reviewer"),
    }
)
# Reviewed source-controlled approval pins, NOT hashes supplied alongside an
# untrusted template. Hashes bind the rules, roles and minimum budget as well.
APPROVED_PINS = MappingProxyType(
    {
        (
            "benchmark-local",
            "1",
        ): "055b8376f011eb411ec4becdb2eed8c26a18d34880444f467b27d33af886fcf6",
        ("research-local", "1"): "e5fba94fb6e80c4af4896e49acb0fbf88ef8b89ad0915b1889ba6f5c8db8688c",
    }
)


def _strict_budget(value: TeamBudget | Mapping) -> TeamBudget:
    """Reject ambiguous numeric input before the shared DTO can coerce it.

    Already normalized DTOs retain only their current values, not the original
    constructor input. Raw request dictionaries are checked before construction.
    """
    if isinstance(value, TeamBudget):
        raw = {name: getattr(value, name) for name in TeamBudget.model_fields}
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise ValueError("budget must be a TeamBudget or mapping")
    for field in BUDGET_FIELDS:
        if field not in raw:
            continue
        number = raw[field]
        if field == "timeout_seconds":
            if type(number) not in (int, float) or (
                type(number) is float and not math.isfinite(number)
            ):
                raise ValueError("finite numeric timeout required")
        elif type(number) is not int:
            raise ValueError("integer budget required without coercion")
    return TeamBudget.model_validate(raw)


class _Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    template: TeamTemplate
    roles: tuple[str, ...]
    goal_terms: tuple[str, ...] = Field(min_length=1)
    outputs: frozenset[str] = Field(min_length=1)
    minimum_budget: TeamBudget

    @model_validator(mode="before")
    @classmethod
    def strict_definition_budgets(cls, value):
        if isinstance(value, dict):
            _strict_budget(value.get("minimum_budget"))
            template = value.get("template")
            if isinstance(template, dict):
                _strict_budget(template.get("budget"))
        return value

    @model_validator(mode="after")
    def consistent_definition(self):
        if self.roles != PATTERN_ROLES[self.template.pattern]:
            raise ValueError("roles must match the approved pattern")
        if not self.template.required_capabilities or len(
            set(self.template.required_capabilities)
        ) != len(self.template.required_capabilities):
            raise ValueError("unique required capabilities are mandatory")
        if any(not value.strip() for value in (*self.goal_terms, *self.outputs)):
            raise ValueError("nonempty goals and outputs required")
        if any(
            getattr(self.minimum_budget, field) > getattr(self.template.budget, field)
            for field in BUDGET_FIELDS
        ):
            raise ValueError("minimum exceeds approved template budget")
        return self


def definition_digest(value: dict) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate registry key")
        result[key] = value
    return result


class TemplateRegistry:
    """Immutable snapshots; requested selections cannot mutate the approved catalog."""

    def __init__(
        self,
        definitions: list[dict] | tuple[dict, ...],
        *,
        approved_pins: Mapping[tuple[str, str], str] = APPROVED_PINS,
    ) -> None:
        try:
            if not isinstance(definitions, (list, tuple)) or not definitions:
                raise ValueError("nonempty definition list required")
            entries, seen = [], set()
            for item in definitions:
                definition = _Definition.model_validate(item)
                key = (definition.template.template_id, definition.template.version)
                fingerprint = definition_digest(item)
                if key in seen or approved_pins.get(key) != fingerprint:
                    raise ValueError("unapproved or modified template")
                seen.add(key)
                entries.append((json.dumps(item, ensure_ascii=False, sort_keys=True), fingerprint))
        except (ValueError, TypeError, KeyError) as exc:
            raise RfaError(
                "invalid_template_registry", "승인된 팀 설정을 확인할 수 없습니다."
            ) from exc
        self._entries = tuple(sorted(entries))

    @classmethod
    def from_file(
        cls, path: Path, *, approved_pins: Mapping[tuple[str, str], str] = APPROVED_PINS
    ) -> TemplateRegistry:
        """Read a server-chosen local configuration, not a caller-supplied path/pin."""
        try:
            if path.stat().st_size > 64_000:
                raise ValueError("oversized registry")
            raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
            if set(raw) != {"registry_version", "templates"} or raw["registry_version"] != "1":
                raise ValueError("unsupported registry version")
            return cls(raw["templates"], approved_pins=approved_pins)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RfaError(
                "invalid_template_registry", "승인된 팀 설정을 확인할 수 없습니다."
            ) from exc

    @classmethod
    def builtin(cls) -> TemplateRegistry:
        return cls.from_file(Path(__file__).resolve().parents[3] / "fixtures/teams/templates.json")

    def definitions(self) -> tuple[tuple[_Definition, str], ...]:
        return tuple(
            (_Definition.model_validate_json(value), digest) for value, digest in self._entries
        )


class SelectionRequest(BaseModel):
    """Untrusted intent/preferences only; no permission or capability claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    goal: str = Field(min_length=1, max_length=10000)
    domain_id: DomainId
    outputs: frozenset[str] = Field(min_length=1)
    requested_pattern: str | None = None
    requested_runtime: str | None = None
    budget: TeamBudget | None = None

    @field_validator("budget", mode="before")
    @classmethod
    def strict_request_budget(cls, value):
        return None if value is None else _strict_budget(value)


@dataclass(frozen=True)
class CandidateRejection:
    template_id: str
    version: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SelectionDecision:
    status: Literal["selected", "unsupported", "denied", "unavailable"]
    reasons: tuple[str, ...]
    template: TeamTemplate | None = None
    definition_digest: str | None = None
    roles: tuple[str, ...] = ()
    execution_budget: TeamBudget | None = None
    rejected: tuple[CandidateRejection, ...] = ()


class TeamSelector:
    def __init__(
        self,
        registry: TemplateRegistry,
        *,
        grants: Mapping[tuple[str, DomainId], frozenset[str]],
        available_capabilities: frozenset[str],
        available_runtimes: frozenset[str],
        budget_ceiling: TeamBudget,
    ) -> None:
        self._registry = registry
        self._grants = {key: frozenset(value) for key, value in grants.items()}
        self._capabilities = frozenset(available_capabilities)
        self._runtimes = frozenset(available_runtimes)
        self._ceiling = _strict_budget(budget_ceiling).model_dump_json()

    def select(self, request: SelectionRequest, principal: TrustedPrincipal) -> SelectionDecision:
        # Revalidate even Pydantic objects modified with model_copy or assignment.
        if not isinstance(request, SelectionRequest) or not isinstance(principal, TrustedPrincipal):
            return SelectionDecision("denied", ("invalid_selection_input",))
        if type(principal.authenticated) is not bool:
            return SelectionDecision("denied", ("invalid_selection_input",))
        try:
            if request.budget is not None:
                _strict_budget(request.budget)
            request = SelectionRequest.model_validate(request.model_dump())
            principal = TrustedPrincipal.model_validate(principal.model_dump(), strict=True)
        except ValueError:
            return SelectionDecision("denied", ("invalid_selection_input",))
        if not principal.authenticated:
            return SelectionDecision("denied", ("authentication_required",))
        granted = self._grants.get((principal.user_id, request.domain_id))
        if granted is None:
            return SelectionDecision("denied", ("domain_permission_missing",))
        if request.requested_pattern is not None and request.requested_pattern not in PATTERN_ROLES:
            return SelectionDecision("unsupported", ("unsupported_pattern",))
        if (
            request.requested_runtime is not None
            and request.requested_runtime not in self._runtimes
        ):
            return SelectionDecision("unavailable", ("requested_runtime_unavailable",))
        ceiling = TeamBudget.model_validate_json(self._ceiling)
        if request.budget and any(
            getattr(request.budget, field) > getattr(ceiling, field) for field in BUDGET_FIELDS
        ):
            return SelectionDecision("denied", ("budget_exceeds_authority",))
        requested_budget = request.budget or ceiling
        candidates, rejected = [], []
        for definition, fingerprint in self._registry.definitions():
            template = definition.template
            required = set(template.required_capabilities)
            reasons = []
            terms = tuple(
                term for term in definition.goal_terms if term.casefold() in request.goal.casefold()
            )
            if not terms:
                reasons.append("goal_not_supported")
            if not request.outputs <= definition.outputs:
                reasons.append("output_not_supported")
            if (
                request.requested_pattern is not None
                and request.requested_pattern != template.pattern
            ):
                reasons.append("pattern_mismatch")
            if not required <= granted:
                reasons.append("capability_permission_missing")
            if not required <= self._capabilities:
                reasons.append("capability_unavailable")
            if template.runtime_kind not in self._runtimes or (
                request.requested_runtime is not None
                and request.requested_runtime != template.runtime_kind
            ):
                reasons.append("runtime_unavailable")
            effective = TeamBudget(
                **{
                    field: min(getattr(requested_budget, field), getattr(template.budget, field))
                    for field in BUDGET_FIELDS
                }
            )
            if any(
                getattr(effective, field) < getattr(definition.minimum_budget, field)
                for field in BUDGET_FIELDS
            ):
                reasons.append("budget_insufficient")
            if reasons:
                rejected.append(
                    CandidateRejection(template.template_id, template.version, tuple(reasons))
                )
                continue
            # Only eligible candidates are ranked; deterministic tie-break by ID/version.
            candidates.append(
                (
                    -len(terms),
                    template.template_id,
                    template.version,
                    definition,
                    fingerprint,
                    effective,
                )
            )
        rejections = tuple(sorted(rejected, key=lambda item: (item.template_id, item.version)))
        if not candidates:
            return SelectionDecision("unavailable", ("no_eligible_team",), rejected=rejections)
        _, _, _, chosen, fingerprint, effective = min(candidates, key=lambda item: item[:3])
        return SelectionDecision(
            "selected",
            (
                "goal_matched",
                "outputs_supported",
                "capabilities_authorized",
                "runtime_available",
                "budget_within_limits",
                "deterministic_rule_selection",
            ),
            chosen.template,
            fingerprint,
            chosen.roles,
            effective,
            rejections,
        )
