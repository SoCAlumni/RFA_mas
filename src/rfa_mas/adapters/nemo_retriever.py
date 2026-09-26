"""Official NVIDIA nemo-retriever Agent Skill as a bounded, read-only Research tool (P1-003).

The NVIDIA `nemo-retriever` Skill (build.nvidia.com/skills, SKILL.md for
`nemo-retriever==26.8.1`) tells an agent to answer only from
`retriever query --format evidence` output and to keep source/page citations. This
adapter runs exactly that CLI command as a subprocess behind ToolPort and maps the
`{evidence, coverage}` shape to product EvidenceItem records.

Resource paths are kept separate:

- hosted embedding (CPU host): LanceDB stays on this PC but the CLI sends the query text to
  NVIDIA's hosted embedding endpoint. Only indexes registered as public AND synthetic may be
  queried this way, because a remote endpoint cannot enforce product ACLs.
- local embedding: a loopback embedding NIM (`--embed-invoke-url`) keeps the query on the PC.
  A GPU/NIM is optional and never required by the default product.

Ingest is an operator step outside the request path; this tool never ingests, writes or
calls the Retriever service/Blueprint. Nothing here is a sandbox: the CLI runs with this
process's OS permissions, a reduced environment and a hard timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import signal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from rfa_mas.contracts import (
    Audience,
    DomainId,
    EvidenceItem,
    ResultStatus,
    SourceLocation,
    StructuredError,
    ToolEffect,
    ToolRequest,
    ToolResult,
    sha256_text,
)
from rfa_mas.contracts.common import OpaqueId
from rfa_mas.errors import RfaError

TOOL_NAME = "nemo_retriever_query"
SKILL_NAME = "nemo-retriever"
SKILL_CLI_VERSION = "26.8.1"
# Parent variables the CLI may need. Proxy variables are never forwarded.
ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
_LOOPBACK_NAMES = {"localhost"}


class RetrieverSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: OpaqueId
    source_revision: OpaqueId


class RetrieverIndex(BaseModel):
    """Operator-registered LanceDB table and the provenance of every ingested file."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    index_id: OpaqueId
    lancedb_uri: str
    table_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    domain_id: DomainId
    audience: Audience
    synthetic: bool
    policy_version: str = Field(min_length=1, max_length=100)
    # CLI "source" value (file basename without .pdf) -> product source identity.
    sources: dict[str, RetrieverSource] = Field(min_length=1)

    @field_validator("lancedb_uri")
    @classmethod
    def local_absolute_path(cls, value: str) -> str:
        if "://" in value or not Path(value).is_absolute():
            raise ValueError("lancedb_uri must be an absolute local path")
        return value

    @classmethod
    def from_manifest(cls, path: Path) -> RetrieverIndex:
        """Load a manifest whose relative `lancedb_dir` is resolved next to the file."""
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("note", None)
        directory = data.pop("lancedb_dir")
        data["lancedb_uri"] = str((path.parent / directory).resolve())
        return cls.model_validate(data)


class RetrieverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    binary: Path
    embedding: Literal["hosted", "local"]
    api_key: SecretStr | None = None
    embed_invoke_url: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_output_bytes: int = Field(default=1_000_000, ge=1024, le=10_000_000)
    max_top_k: int = Field(default=5, ge=1, le=20)
    max_query_chars: int = Field(default=500, ge=1, le=4000)
    excerpt_chars: int = Field(default=1000, ge=80, le=20_000)

    @model_validator(mode="after")
    def consistent_path(self) -> RetrieverConfig:
        if not self.binary.is_absolute():
            raise ValueError("retriever binary must be an absolute path")
        if self.embedding == "hosted":
            if self.api_key is None or not self.api_key.get_secret_value():
                raise ValueError("hosted embedding requires NVIDIA_API_KEY")
            if self.embed_invoke_url is not None:
                raise ValueError("hosted embedding uses the CLI default endpoint")
        else:
            if self.api_key is not None:
                raise ValueError("local embedding must not receive the hosted API key")
            if not self.embed_invoke_url or not _is_loopback_url(self.embed_invoke_url):
                raise ValueError("local embedding requires a loopback embed_invoke_url")
        return self


def _is_loopback_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    if parts.hostname in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        return False


class _Locator(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["page", "segment", "timestamp", "bbox"]
    value: int | float | list[float] | None


class _CliEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    text: str
    source: str
    locator: _Locator
    modality: str
    fidelity: str
    score: float | int
    citation: str


class _CliCoverage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    strategies_used: list[str]
    n_docs_seen: int = Field(ge=0)
    thin_spots: list[str]


class _CliResult(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    evidence: list[_CliEvidence]
    coverage: _CliCoverage


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1)
    index_id: OpaqueId
    top_k: int = Field(default=3, ge=1)


class _ToolFailure(Exception):
    def __init__(self, status: ResultStatus, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


_MESSAGES = {
    "tool_not_allowed": "허용되지 않은 도구 요청입니다.",
    "invalid_tool_arguments": "도구 인자가 유효하지 않습니다.",
    "unknown_index": "등록되지 않은 검색 색인입니다.",
    "egress_not_permitted": "이 색인은 원격 embedding 경로로 질의할 수 없습니다.",
    "retriever_unavailable": "retriever 실행 파일을 사용할 수 없습니다.",
    "retriever_timeout": "retriever 실행 시간이 제한을 넘었습니다.",
    "retriever_failed": "retriever 실행이 실패했습니다.",
    "output_too_large": "retriever 출력이 허용 크기를 넘었습니다.",
    "malformed_evidence": "retriever evidence 형식이 올바르지 않습니다.",
}


def _fingerprint(request: ToolRequest) -> str:
    raw = json.dumps(request.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class NemoRetrieverTool:
    """ToolPort for one tool name: `nemo_retriever_query` (READ only)."""

    adapter_name = "nemo-retriever-cli"
    simulated = False

    def __init__(self, config: RetrieverConfig, indexes: tuple[RetrieverIndex, ...]) -> None:
        ids = [index.index_id for index in indexes]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("retriever indexes must be non-empty and unique")
        self.config = config
        self.indexes = {index.index_id: index for index in indexes}
        self._idempotency: dict[str, tuple[str, ToolResult]] = {}
        self._lock = asyncio.Lock()

    def egress_permitted(self, index: RetrieverIndex) -> bool:
        if self.config.embedding == "local":
            return True
        return index.audience == Audience.PUBLIC and index.synthetic

    def query_permitted(self, query: str) -> bool:
        """The hosted embedding is a cloud model: private markers never leave the PC.

        Same deterministic markers as the P1-005 content screen (application.graphs.domain).
        """
        if self.config.embedding == "local":
            return True
        from rfa_mas.application.graphs.domain import SENSITIVE_MARKERS

        return not any(pattern.search(query) for pattern in SENSITIVE_MARKERS)

    async def execute(self, request: ToolRequest) -> ToolResult:
        request = ToolRequest.model_validate_json(request.model_dump_json())
        fingerprint = _fingerprint(request)
        async with self._lock:
            cached = self._idempotency.get(request.idempotency_key)
        if cached is not None:
            if cached[0] != fingerprint:
                raise RfaError("idempotency_conflict", "같은 idempotency key로 다른 요청입니다.")
            return cached[1]
        try:
            output, status, code = await self._run(request), ResultStatus.SUCCEEDED, None
        except _ToolFailure as failure:
            output, status, code = {}, failure.status, failure.code
        result = ToolResult(
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            agent_id=request.agent_id,
            domain_id=request.domain_id,
            idempotency_key=request.idempotency_key,
            status=status,
            output=output,
            error=StructuredError(
                code=code,
                message=_MESSAGES[code],
                retryable=code == "retriever_timeout",
                request_id=request.request_id,
                trace_id=request.trace_id,
                run_id=request.run_id,
            )
            if code
            else None,
            simulated=self.simulated,
            adapter=self.adapter_name,
        )
        async with self._lock:
            self._idempotency.setdefault(request.idempotency_key, (fingerprint, result))
        return result

    async def _run(self, request: ToolRequest) -> dict[str, Any]:
        if request.tool_name != TOOL_NAME or request.effect != ToolEffect.READ:
            raise _ToolFailure(ResultStatus.DENIED, "tool_not_allowed")
        try:
            args = _Arguments.model_validate(request.arguments)
        except ValidationError:
            raise _ToolFailure(ResultStatus.FAILED, "invalid_tool_arguments") from None
        if len(args.query) > self.config.max_query_chars or args.top_k > self.config.max_top_k:
            raise _ToolFailure(ResultStatus.FAILED, "invalid_tool_arguments")
        index = self.indexes.get(args.index_id)
        if index is None or index.domain_id != request.domain_id:
            raise _ToolFailure(ResultStatus.DENIED, "unknown_index")
        if not self.egress_permitted(index) or not self.query_permitted(args.query):
            # Decided before any subprocess: the query text never leaves the PC.
            raise _ToolFailure(ResultStatus.DENIED, "egress_not_permitted")
        stdout = await self._invoke(self._argv(index, args))
        parsed = _parse(stdout)
        return self._map(index, parsed)

    def _argv(self, index: RetrieverIndex, args: _Arguments) -> list[str]:
        argv = [
            str(self.config.binary),
            "query",
            args.query,
            "--lancedb-uri",
            index.lancedb_uri,
            "--table-name",
            index.table_name,
            "--top-k",
            str(args.top_k),
            "--format",
            "evidence",
        ]
        if self.config.embedding == "local":
            argv += ["--embed-invoke-url", str(self.config.embed_invoke_url)]
        return argv

    def _env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
        if self.config.embedding == "hosted" and self.config.api_key is not None:
            env["NVIDIA_API_KEY"] = self.config.api_key.get_secret_value()
        return env

    async def _invoke(self, argv: list[str]) -> bytes:
        if not os.access(self.config.binary, os.X_OK):
            raise _ToolFailure(ResultStatus.FAILED, "retriever_unavailable")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._env(),
                start_new_session=True,
            )
        except OSError:
            raise _ToolFailure(ResultStatus.FAILED, "retriever_unavailable") from None
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                stdout = await self._read_limited(process)
                returncode = await process.wait()
        except TimeoutError:
            raise _ToolFailure(ResultStatus.TIMED_OUT, "retriever_timeout") from None
        finally:
            await _terminate(process)
        if returncode != 0:
            # stderr is discarded: it may echo the query or environment details.
            raise _ToolFailure(ResultStatus.FAILED, "retriever_failed")
        return stdout

    async def _read_limited(self, process: asyncio.subprocess.Process) -> bytes:
        assert process.stdout is not None
        chunks, total = [], 0
        while chunk := await process.stdout.read(65536):
            total += len(chunk)
            if total > self.config.max_output_bytes:
                raise _ToolFailure(ResultStatus.FAILED, "output_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _map(self, index: RetrieverIndex, parsed: _CliResult) -> dict[str, Any]:
        evidence, citations, unmapped = [], [], 0
        for item in parsed.evidence:
            source = index.sources.get(item.source)
            if source is None:
                unmapped += 1  # Unregistered provenance is never presented as evidence.
                continue
            page = item.locator.value if item.locator.kind == "page" else None
            if page is not None and (type(page) is not int or page < 1):
                raise _ToolFailure(ResultStatus.FAILED, "malformed_evidence")
            kind, value = item.locator.kind, item.locator.value
            section = None if kind == "page" else f"{kind}:{value}"
            evidence.append(
                EvidenceItem(
                    source_id=source.source_id,
                    source_revision=source.source_revision,
                    location=SourceLocation(
                        uri=f"nemo-retriever://{index.index_id}/{item.source}",
                        section=section,
                        page=page,
                    ),
                    audience=index.audience,
                    excerpt=item.text[: self.config.excerpt_chars],
                    content_hash=sha256_text(item.text),
                    policy_version=index.policy_version,
                ).model_dump(mode="json")
            )
            citations.append(
                {
                    "source_id": source.source_id,
                    "citation": item.citation,
                    "fidelity": item.fidelity,
                    "modality": item.modality,
                    "score": float(item.score),
                }
            )
        return {
            "skill": {"name": SKILL_NAME, "cli_version": SKILL_CLI_VERSION},
            "index_id": index.index_id,
            "embedding": self.config.embedding,
            "evidence": evidence,
            "citations": citations,
            "coverage": parsed.coverage.model_dump(mode="json"),
            "unmapped": unmapped,
            "insufficient": not evidence,
        }


def _parse(stdout: bytes) -> _CliResult:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise _ToolFailure(ResultStatus.FAILED, "malformed_evidence") from None
    # Log lines may precede the JSON document; nothing but whitespace may follow it.
    starts = [0] if text.startswith("{") else []
    starts += [i + 1 for i, char in enumerate(text) if char == "\n" and text[i + 1 : i + 2] == "{"]
    for start in starts:
        try:
            data, end = json.JSONDecoder().raw_decode(text, start)
        except ValueError:
            continue
        if text[end:].strip():
            break
        try:
            return _CliResult.model_validate(data)
        except ValidationError:
            break
    raise _ToolFailure(ResultStatus.FAILED, "malformed_evidence")


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await asyncio.shield(process.wait())


class RetrieverToolRouter:
    """Route only the retriever tool name to the retriever; all else to the base tools."""

    def __init__(self, base: Any, retriever: NemoRetrieverTool) -> None:
        self.base, self.retriever = base, retriever
        self.adapter_name = f"{base.adapter_name}+{retriever.adapter_name}"
        self.simulated = bool(base.simulated or retriever.simulated)

    async def execute(self, request: ToolRequest) -> ToolResult:
        target = self.retriever if request.tool_name == TOOL_NAME else self.base
        return await target.execute(request)


__all__ = [
    "SKILL_CLI_VERSION",
    "TOOL_NAME",
    "NemoRetrieverTool",
    "RetrieverConfig",
    "RetrieverIndex",
    "RetrieverSource",
    "RetrieverToolRouter",
]
