from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from rfa_mas.adapters.checkpoints import SqliteCheckpoints
from rfa_mas.adapters.http import (
    PolicyHttpAdapter,
    PublicationHttpAdapter,
    ReferenceHttpClient,
    ResponseHttpAdapter,
    RuntimeHttpAdapter,
    ToolHttpAdapter,
    require_loopback_reference_url,
)
from rfa_mas.adapters.langfuse import (
    LangfuseEgress,
    LangfuseExportTrace,
    LangfuseOtlpExporter,
    LangfuseRetentionSweeper,
    RetentionSweepReport,
)
from rfa_mas.adapters.local import (
    LocalAnalysisTools,
    LocalJsonlTrace,
    LocalPolicy,
    LocalRuntime,
    SqliteWorkRepository,
)
from rfa_mas.adapters.mock import (
    MockJudge,
    MockModel,
    MockPublisher,
    MockResponse,
    MockRetrieval,
    MockTool,
)
from rfa_mas.adapters.nemo_retriever import (
    TOOL_NAME as SKILL_TOOL_NAME,
)
from rfa_mas.adapters.nemo_retriever import (
    NemoRetrieverTool,
    RetrieverConfig,
    RetrieverIndex,
    RetrieverToolRouter,
)
from rfa_mas.adapters.scheduler import ApschedulerTriggers, ManualClock, SchedulerRunner
from rfa_mas.application.drafts import DraftLifecycle
from rfa_mas.application.events import EventFeed
from rfa_mas.application.feedback import FeedbackService
from rfa_mas.adapters.retrieval import BoundContextReader, LocalRetrieval
from rfa_mas.application.source_access import BoundAccess, ProjectResolver, no_projects
from rfa_mas.application.graphs import (
    DomainGraphDependencies,
    SupervisorDependencies,
    build_domain_task_handler,
)
from rfa_mas.application.knowledge import KnowledgeService
from rfa_mas.application.observations import Observations, ObservedPort
from rfa_mas.application.resume_policy import ResumePolicy
from rfa_mas.application.candidates import CandidateService
from rfa_mas.application.scheduling import (
    LedgerScheduledEffectHook,
    ScheduleExecutor,
    ScheduleService,
)
from rfa_mas.application.service import WorkService
from rfa_mas.application.team_selector import APPROVED_PINS, TeamSelector, TemplateRegistry
from rfa_mas.application.teams import RuntimeLifecycleSupport, TeamFactory
from rfa_mas.application.workers import TEAM_ROLE_TASK, ExternalSearchBinding, TeamRunner
from rfa_mas.contracts import (
    AdapterInfo,
    DomainId,
    ExecutionMode,
    KnowledgeDocument,
    ReadinessCheck,
    ReadinessReport,
    TeamBudget,
    TrustedPrincipal,
)
from rfa_mas.errors import BackendNotImplementedError, ConfigurationError
from rfa_mas.security import SecretRedactor, configure_logging
from rfa_mas.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_NVIDIA_SETTING_NAMES = {
    "base_url": "NVIDIA_BASE_URL",
    "model": "NVIDIA_MODEL",
    "api_key": "NVIDIA_API_KEY",
}


def _nvidia_config(settings: Settings):
    """P1-002: validated NVIDIA chat config, or ValueError naming the invalid setting."""
    from pydantic import ValidationError

    from rfa_mas.adapters.nvidia import NvidiaChatConfig

    try:
        return NvidiaChatConfig(
            base_url=settings.nvidia_base_url,
            model=settings.nvidia_model or "",
            api_key=settings.nvidia_api_key,
            timeout_seconds=settings.http_timeout_seconds,
        )
    except ValidationError as exc:
        locs = {str(error["loc"][0]) for error in exc.errors() if error.get("loc")}
        names = sorted(_NVIDIA_SETTING_NAMES.get(loc, "NVIDIA_MODEL") for loc in locs)
        raise ValueError(",".join(names)) from None


def _judge_config(settings: Settings):
    """P1-006A: validated Judge chat config (JUDGE_MODEL over the NVIDIA endpoint/key)."""
    from pydantic import ValidationError

    from rfa_mas.adapters.nvidia import NvidiaChatConfig

    try:
        return NvidiaChatConfig(
            base_url=settings.nvidia_base_url,
            model=settings.judge_model or "",
            api_key=settings.nvidia_api_key,
            timeout_seconds=settings.http_timeout_seconds,
        )
    except ValidationError as exc:
        locs = {str(error["loc"][0]) for error in exc.errors() if error.get("loc")}
        names = sorted(
            (_NVIDIA_SETTING_NAMES | {"model": "JUDGE_MODEL"}).get(loc, "JUDGE_MODEL")
            for loc in locs
        )
        raise ValueError(",".join(names)) from None


@dataclass(frozen=True)
class ConfigurationInspection:
    missing: tuple[str, ...]
    invalid: tuple[str, ...]
    reserved: tuple[str, ...]
    nat_dependency: str

    @property
    def configuration_ready(self) -> bool:
        return not self.missing and not self.invalid

    @property
    def implementation_ready(self) -> bool:
        return not self.reserved

    @property
    def ready(self) -> bool:
        return self.configuration_ready and self.implementation_ready

    def require_available(self) -> None:
        if self.missing or self.invalid:
            raise ConfigurationError([*self.missing, *self.invalid])
        if self.reserved:
            raise BackendNotImplementedError(self.reserved[0])

    def check(self) -> ReadinessCheck:
        """Exact names/codes of what blocks the selected modes; never a value."""
        code = (
            "configuration_error"
            if self.missing or self.invalid
            else "not_implemented"
            if self.reserved
            else "ok"
        )
        return ReadinessCheck(
            component="configuration",
            selected="selected_modes",
            ready=self.ready,
            code=code,
            missing=self.missing,
            invalid=self.invalid,
            reserved=self.reserved,
        )


def readiness_report(checks: list[ReadinessCheck]) -> ReadinessReport:
    ready = bool(checks) and all(check.ready for check in checks)
    return ReadinessReport(
        status="ready" if ready else "not_ready", ready=ready, checks=tuple(checks)
    )


def unavailable_readiness(settings: Settings) -> ReadinessReport:
    """Readiness of a process whose container could not be built for the selected modes."""
    return readiness_report(
        [
            ReadinessCheck(component="service", selected="local", ready=False, code="not_started"),
            inspect_configuration(settings).check(),
        ]
    )


def inspect_configuration(settings: Settings) -> ConfigurationInspection:
    """No clients, DBs, model imports, network probes or user values in the report."""
    missing = settings.missing_for_selected_modes()
    invalid: list[str] = []
    reserved = list(settings.selected_reserved_features())
    try:
        settings.ensure_ready()
        _ = settings.resolved_checkpoint_path
    except ConfigurationError as exc:
        invalid.extend(name for name in exc.missing if name not in missing)
    except (ValueError, OSError):
        invalid.append("DATABASE_URL/CHECKPOINT_PATH")
    try:
        _ = settings.scheduler_jobstore_path
    except (ConfigurationError, ValueError, OSError):
        invalid.append("SCHEDULER_JOBSTORE_URL")
    for name, mode, url in (
        ("RESPONSE_BASE_URL", settings.response_backend, settings.response_base_url),
        ("TOOL_BASE_URL", settings.tool_backend, settings.tool_base_url),
        ("RUNTIME_BASE_URL", settings.runtime_backend, settings.runtime_base_url),
        ("POLICY_BASE_URL", settings.policy_backend, settings.policy_base_url),
        # P1-006C: same URL-shape and loopback gate for the Langfuse trace exporter.
        (
            "LANGFUSE_BASE_URL",
            "http" if settings.trace_backend == "langfuse" else "off",
            settings.langfuse_base_url,
        ),
    ):
        if mode != "http" or not url:
            continue
        try:
            # HTTPX may accept an unmatched '[' as a hostname. Validate URL shape
            # and port without exposing parser messages before classifying locality.
            structure = urlsplit(url)
            _ = structure.port
            if not structure.hostname:
                invalid.append(name)
                continue
            parsed = httpx.URL(url)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.userinfo
                or parsed.query
                or parsed.fragment
            ):
                invalid.append(name)
                continue
            require_loopback_reference_url(url, setting_name=name)
        except (ValueError, httpx.InvalidURL):
            invalid.append(name)
        except BackendNotImplementedError:
            reserved.append(f"endpoint:{name}:non_loopback")
    for port, selected, default in (
        ("judge", settings.judge_provider if settings.enable_judge else "mock", "mock"),
    ):
        if selected != default and selected != "nvidia":
            reserved.append(f"{port}:{selected}")
    # P1-006A: an enabled nvidia Judge exists; a configured selection must also be valid.
    if (
        settings.enable_judge
        and settings.judge_provider == "nvidia"
        and not {
            "JUDGE_MODEL",
            "NVIDIA_API_KEY",
        }
        & set(missing)
    ):
        try:
            _judge_config(settings)
        except ValueError as exc:
            invalid.extend(str(exc).split(","))
    # P1-002: the NVIDIA ModelPort adapter exists; a configured selection must also be valid.
    if settings.model_provider == "nvidia" and not {"NVIDIA_MODEL", "NVIDIA_API_KEY"} & set(
        missing
    ):
        try:
            _nvidia_config(settings)
        except ValueError as exc:
            invalid.extend(str(exc).split(","))
    if settings.retriever_backend not in {"local", "mock", "nemo_cli"}:
        reserved.append(f"retriever:{settings.retriever_backend}")
    # P1-003: report an invalid nemo_cli selection by name instead of only failing at build.
    if settings.retriever_backend == "nemo_cli" and not {
        "RETRIEVER_CLI_PATH",
        "NVIDIA_API_KEY",
    } & set(missing):
        try:
            _nemo_cli(settings)
        except ConfigurationError as exc:
            invalid.extend(exc.missing)
    # Installed metadata is not an import/compatibility check or product NAT integration.
    try:
        metadata.version("nvidia-nat-langchain")
        nat_dependency = "installed_unverified"
    except metadata.PackageNotFoundError:
        nat_dependency = "missing"
    if settings.enable_nat:
        if nat_dependency == "missing":
            missing.append("NAT_EXTRA")
        reserved.append("feature:nat_adapter")
    return ConfigurationInspection(
        tuple(sorted(set(missing))),
        tuple(sorted(set(invalid))),
        tuple(sorted(set(reserved))),
        nat_dependency,
    )


@dataclass
class Container:
    settings: Settings
    repository: SqliteWorkRepository
    service: WorkService
    model: object
    retrieval: object
    response: object
    tool: object
    runtime: object
    policy: object
    trace: object
    judge: object
    adapters: tuple[AdapterInfo, ...]
    checkpoints: SqliteCheckpoints
    team_factory: TeamFactory
    knowledge: KnowledgeService
    team_runner: TeamRunner | None = None
    # P1-005A: DRAFT versions/approval validity/publication receipts (mock publisher only).
    drafts: DraftLifecycle | None = None
    # P1-005C: staged-context retrieval boundary (None when the runtime is not local).
    context: StagedContextBoundary | None = None
    # P0-022: owner schedule intent only. The API never starts or opens the scheduler runner.
    schedules: ScheduleService | None = None
    http_clients: list[httpx.AsyncClient] = field(default_factory=list)
    # P0-025: selected reference HTTP backends probed by readiness, and the event feed.
    readiness_probes: dict[str, ReferenceHttpClient] = field(default_factory=dict)
    events: EventFeed | None = None
    ready: bool = False

    def context_reader(self, bound: BoundAccess) -> BoundContextReader:
        """Internal trusted composition, not a request-body factory or role grant."""
        return BoundContextReader(
            self.repository,
            self.policy,
            bound,
            issuer_supported=self.settings.policy_backend == "local",
        )

    async def service_owner_id(self) -> str:
        """Installation owner injected into local stand-ins (P1-008C/D boundaries).

        Server-verified identity from the repository, never a request/body claim.
        """
        return (await self.repository.local_principal()).user_id

    async def startup(self) -> None:
        if self.ready:
            return
        try:
            await self.repository.initialize()
            documents = _load_documents(PROJECT_ROOT / "fixtures" / "documents")
            await self.repository.seed_documents_once(documents)
            self.service.start(await self.checkpoints.start())
        except BaseException:
            await self.shutdown()
            raise
        self.ready = True

    async def shutdown(self) -> None:
        self.ready = False
        self.service.stop()
        await self.checkpoints.close()
        for client in self.http_clients:
            await client.aclose()
        close_model = getattr(self.model, "aclose", None)
        if close_model is not None:
            await close_model()
        close_judge = getattr(self.judge, "aclose", None)
        if close_judge is not None:
            await close_judge()
        self.ready = False

    async def readiness(self) -> ReadinessReport:
        """Liveness is /healthz. Ready needs a started service, a complete configuration
        for every selected mode and a reachable /healthz on each selected HTTP backend."""
        checks = [
            ReadinessCheck(
                component="service",
                selected="local",
                ready=self.ready,
                code="ok" if self.ready else "not_started",
            ),
            inspect_configuration(self.settings).check(),
        ]
        timeout = min(self.settings.http_timeout_seconds, 2.0)
        for name, probe in sorted(self.readiness_probes.items()):
            reachable = False
            try:
                # No credential is sent, nothing is retried, no response text is kept.
                response = await probe.client.get("/healthz", timeout=timeout)
                reachable = response.status_code == 200
            except (httpx.HTTPError, OSError):
                reachable = False
            checks.append(
                ReadinessCheck(
                    component=name.removesuffix("_BASE_URL").lower(),
                    selected="http",
                    ready=reachable,
                    code="ok" if reachable else "unavailable",
                )
            )
        return readiness_report(checks)


def _load_documents(directory: Path) -> list[KnowledgeDocument]:
    documents: list[KnowledgeDocument] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                documents.append(KnowledgeDocument.model_validate(json.loads(line)))
            except Exception as exc:
                raise ValueError(f"Invalid fixture {path}:{line_number}") from exc
    return documents


def _nemo_cli(settings: Settings):
    """P1-003: validated Skill CLI config and registered index, or the invalid setting name."""
    try:
        config = RetrieverConfig(
            binary=settings.retriever_cli_path or Path(),
            embedding="hosted",
            api_key=settings.nvidia_api_key,
        )
    except ValueError:
        raise ConfigurationError(["RETRIEVER_CLI_PATH"]) from None
    try:
        index = RetrieverIndex.from_manifest(settings.retriever_index_dir / "index_manifest.json")
    except (OSError, ValueError, KeyError):
        raise ConfigurationError(["RETRIEVER_INDEX_DIR"]) from None
    return config, index


def _team_tools(settings: Settings, observer: Observations):
    """Team role tools: local READ computations, plus the Skill tool for nemo_cli (P1-003)."""
    local = ObservedPort(LocalAnalysisTools(), observer, "tool", mode="local")
    if settings.retriever_backend != "nemo_cli":
        return local, None
    config, index = _nemo_cli(settings)
    skill = ObservedPort(NemoRetrieverTool(config, (index,)), observer, "tool", mode="real")
    binding = ExternalSearchBinding(SKILL_TOOL_NAME, index.index_id, index.audience)
    return RetrieverToolRouter(local, skill), binding


def _http_port(
    *,
    base_url: str,
    token: object,
    settings: Settings,
    clients: list[httpx.AsyncClient],
    endpoint_name: str,
    transport: httpx.AsyncBaseTransport | None = None,
    probes: dict[str, ReferenceHttpClient] | None = None,
) -> ReferenceHttpClient:
    require_loopback_reference_url(base_url, setting_name=endpoint_name)
    client = httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        # Optional injected transport (ASGI/MockTransport in tests, local stack). The
        # loopback URL check above still applies; the transport never widens egress.
        transport=transport,
    )
    reference_client = ReferenceHttpClient(
        client,
        token=token,
        max_read_retries=settings.max_read_retries,
        endpoint_name=endpoint_name,
    )
    clients.append(client)
    if probes is not None:
        probes[endpoint_name] = reference_client
    return reference_client


def _langfuse_trace(
    local_trace: LocalJsonlTrace,
    settings: Settings,
    redactor: SecretRedactor,
    clients: list[httpx.AsyncClient],
    transport: httpx.AsyncBaseTransport | None,
) -> LangfuseExportTrace:
    """P1-006C: local JSONL stays the source of truth; Langfuse export is additive.

    Readiness already requires the keys, LANGFUSE_EXPORT_ENABLED=true and a loopback
    URL. The exporter re-checks the same egress permission before every request.
    """
    base_url = settings.langfuse_base_url or ""
    require_loopback_reference_url(base_url, setting_name="LANGFUSE_BASE_URL")
    timeout = min(settings.http_timeout_seconds, 5.0)
    client = httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(timeout),
        # Optional injected transport (MockTransport in tests). The loopback gate above
        # and the per-request egress check still apply; no redirects are followed.
        transport=transport,
        follow_redirects=False,
    )
    clients.append(client)
    exporter = LangfuseOtlpExporter(
        client=client,
        egress=LangfuseEgress(base_url, settings.langfuse_export_enabled),
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        redactor=redactor,
        timeout_seconds=timeout,
    )
    return LangfuseExportTrace(local_trace, exporter)


async def run_langfuse_retention(
    settings: Settings,
    *,
    dry_run: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    **sweeper_options,
) -> RetentionSweepReport:
    """P1-006F: one self-run retention sweep against the loopback community Langfuse.

    Uses the same egress permission as the exporter (loopback LANGFUSE_BASE_URL, keys and
    LANGFUSE_EXPORT_ENABLED=true). Only this app's exported traces are ever deleted; local
    JSONL traces are untouched; TRACE_RETENTION_DAYS governs both stores' retention.
    """
    base_url = settings.langfuse_base_url or ""
    egress = LangfuseEgress(base_url, settings.langfuse_export_enabled)
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/") if egress.permitted() else "http://127.0.0.1:9",
        timeout=httpx.Timeout(min(settings.http_timeout_seconds, 10.0)),
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        sweeper = LangfuseRetentionSweeper(
            client=client,
            egress=egress,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            retention_days=settings.trace_retention_days,
            **sweeper_options,
        )
        return await sweeper.sweep(dry_run=dry_run)


def _staged_context(
    repository, policy, settings: Settings, observer: Observations, on_context=None
):
    """Trusted factory (P1-005): staged L0/L1/L2 context for owner/public local targets.

    Returns None when the target/endpoint is outside the bound reader's supported scope,
    so the graph uses the ACL-bound retrieval path and the share/egress screen instead.
    """
    from time import perf_counter

    from rfa_mas.application.context import ContextLoader
    from rfa_mas.contracts import (
        Audience,
        ContextRequest,
        DraftTarget,
        EvidenceBundle,
        EvidenceItem,
    )

    async def load(principal, work, request):
        target = work.target.audience
        if target not in {Audience.OWNER, Audience.PUBLIC} or settings.policy_backend != "local":
            return None
        bound_target = DraftTarget(audience=target)
        bound = BoundAccess(
            lambda: principal,
            request.agent_id,
            "supervisor",
            request.domain_id,
            bound_target,
            "mock-model",
        )
        loader = ContextLoader(
            repository, BoundContextReader(repository, policy, bound, issuer_supported=True)
        )
        context_request = ContextRequest.model_validate(
            request.model_dump(exclude={"schema_version"})
            | {
                "goal": work.query,
                "role": "supervisor",
                "target": bound_target,
                "endpoint_id": "mock-model",
            }
        )
        await observer.record("retrieval", "started", mode="local")
        start = perf_counter()
        try:
            loaded = await loader.load(context_request)
        except BaseException:
            await observer.record(
                "retrieval",
                "failed",
                mode="local",
                reason="provider_error",
                duration_ms=(perf_counter() - start) * 1000,
                transport="raised",
            )
            raise
        items = tuple(
            EvidenceItem(
                source_id=i.source_id,
                source_revision=i.source_revision,
                location=i.location,
                audience=i.audience,
                excerpt=i.excerpt,
                content_hash=i.content_hash,
                policy_version=i.policy_version,
            )
            for i in loaded.bundle.items
        )
        await observer.record(
            "retrieval",
            "succeeded",
            mode="local",
            duration_ms=(perf_counter() - start) * 1000,
            sources=tuple((i.source_id, i.source_revision) for i in items),
            transport="returned",
        )
        if on_context is not None:
            # P0-025: durable Run-linked stage outcomes/source refs (no text) for polling.
            await on_context(principal, request.run_id, loaded)
        evidence = EvidenceBundle(
            request_id=request.request_id,
            trace_id=request.trace_id,
            run_id=request.run_id,
            agent_id=request.agent_id,
            domain_id=request.domain_id,
            items=items,
            insufficient=loaded.insufficient,
            policy_version=policy.policy_version,
            simulated=False,
            adapter="staged-context-v1",
        )
        stats = {
            "loader": "staged-context-v1",
            "budget_characters": loaded.budget_characters,
            "mandatory_characters": loaded.mandatory_characters,
            "loaded_characters": loaded.bundle.loaded_characters,
            "summaries": loaded.summaries,
            "reason": loaded.reason,
            "records": [
                {"stage": r.stage.value, "outcome": r.outcome, "characters": r.characters}
                for r in loaded.records
            ],
        }
        return evidence, stats

    return load


class StagedContextBoundary:
    """P1-005C: the staged-context retrieval boundary as a container-owned component.

    The domain graph calls `load` through late attribute lookup (like the ObservedPort
    wrappers do for adapters), so an isolated evaluation harness can observe this exact
    boundary on its own private container. It adds no capability and no fallback.
    """

    adapter_name = "staged-context-v1"
    simulated = False

    def __init__(self, load) -> None:
        self._load = load

    async def load(self, principal, work, request):
        return await self._load(principal, work, request)


def build_container(
    settings: Settings | None = None,
    *,
    project_resolver: ProjectResolver = no_projects,
    http_transport: httpx.AsyncBaseTransport | None = None,
    model_transport: httpx.AsyncBaseTransport | None = None,
    trace_transport: httpx.AsyncBaseTransport | None = None,
) -> Container:
    settings = settings or Settings()
    inspect_configuration(settings).require_available()
    redactor = SecretRedactor(settings.secret_values())
    configure_logging(settings.log_level, redactor)
    repository = SqliteWorkRepository(settings.database_path, project_resolver=project_resolver)
    checkpoints = SqliteCheckpoints(settings.resolved_checkpoint_path)
    clients: list[httpx.AsyncClient] = []
    probes: dict[str, ReferenceHttpClient] = {}

    if settings.model_provider == "mock":
        model = MockModel()
    else:
        # P1-002: explicit selection only (inspect_configuration already required key and
        # model). No mock fallback. Egress needs the trusted public-only gate per call.
        from rfa_mas.adapters.nvidia import (
            NvidiaChatModel,
            OwnerConsentEgressGate,
            PublicOnlyEgressGate,
        )

        nvidia = _nvidia_config(settings)
        # P1-008K: ALLOW_EXTERNAL_EGRESS is the owner's consent for owner-target context.
        gate_class = (
            OwnerConsentEgressGate if settings.external_egress_effective else PublicOnlyEgressGate
        )
        model = NvidiaChatModel(
            nvidia,
            gate_class(
                endpoint=nvidia.endpoint,
                model=nvidia.model,
                max_output_tokens=settings.nvidia_max_output_tokens,
                # Bounded by the domain task's own timeout (LocalRuntime wait_for).
                budget_seconds=settings.tool_timeout_seconds,
            ),
            transport=model_transport,
        )

    if settings.policy_backend == "local":
        policy = LocalPolicy()
    else:
        policy = PolicyHttpAdapter(
            _http_port(
                base_url=settings.policy_base_url or "",
                token=settings.policy_api_token,
                settings=settings,
                clients=clients,
                endpoint_name="POLICY_BASE_URL",
                transport=http_transport,
                probes=probes,
            )
        )

    # nemo_cli keeps the ACL-checked KB reader local; the official Skill is a Research tool.
    if settings.retriever_backend in {"local", "nemo_cli"}:
        retrieval = LocalRetrieval(repository, policy_version=policy.policy_version)
    elif settings.retriever_backend == "mock":
        retrieval = MockRetrieval(repository, policy_version=policy.policy_version)
    else:
        raise BackendNotImplementedError(f"retriever:{settings.retriever_backend}")

    if settings.response_backend == "mock":
        response = MockResponse()
        response_client = None
    else:
        response_client = _http_port(
            base_url=settings.response_base_url or "",
            token=settings.response_api_token,
            settings=settings,
            clients=clients,
            endpoint_name="RESPONSE_BASE_URL",
            transport=http_transport,
            probes=probes,
        )
        response = ResponseHttpAdapter(response_client)

    if settings.tool_backend == "mock":
        tool = MockTool()
    else:
        tool = ToolHttpAdapter(
            _http_port(
                base_url=settings.tool_base_url or "",
                token=settings.tool_api_token,
                settings=settings,
                clients=clients,
                endpoint_name="TOOL_BASE_URL",
                transport=http_transport,
                probes=probes,
            )
        )

    if settings.runtime_backend == "local":
        runtime = LocalRuntime(timeout_seconds=settings.tool_timeout_seconds)
    else:
        runtime = RuntimeHttpAdapter(
            _http_port(
                base_url=settings.runtime_base_url or "",
                token=settings.runtime_api_token,
                settings=settings,
                clients=clients,
                endpoint_name="RUNTIME_BASE_URL",
                transport=http_transport,
                probes=probes,
            )
        )

    local_trace = LocalJsonlTrace(
        settings.trace_dir,
        redactor,
        repository=repository,
        retention_days=settings.trace_retention_days,
    )
    trace: LocalJsonlTrace | LangfuseExportTrace = local_trace
    if settings.trace_backend == "langfuse":
        trace = _langfuse_trace(local_trace, settings, redactor, clients, trace_transport)
    observer = Observations(repository, trace, policy=policy)
    runtime_support = RuntimeLifecycleSupport(
        supported=isinstance(runtime, LocalRuntime),
        runtime_kind="local",
        expected_mode=ExecutionMode.LOCAL,
    )

    async def resolve_team_selector(principal: TrustedPrincipal, domain: DomainId) -> TeamSelector:
        # Server-owned installation identity, not role strings in user text.
        # Reload approved definitions/pins and current limits for EVERY invocation.
        local_owner = await repository.local_principal()
        capabilities = frozenset(
            {"evidence_search", "experiment_run", "result_analysis", "evidence_review"}
        )
        grants = {(local_owner.user_id, d): capabilities for d in DomainId}
        return TeamSelector(
            TemplateRegistry.from_file(
                PROJECT_ROOT / "fixtures/teams/templates.json", approved_pins=APPROVED_PINS
            ),
            grants=grants,
            available_capabilities=capabilities,
            available_runtimes=frozenset({"local"}) if runtime_support.supported else frozenset(),
            budget_ceiling=TeamBudget(
                max_steps=settings.max_graph_steps,
                max_tool_calls=settings.max_tool_calls,
                timeout_seconds=settings.tool_timeout_seconds,
            ),
        )

    # Lifecycle gets the configured port, NOT ObservedPort (whose methods do not
    # prove inner capability). Lifecycle is durable DB evidence, trace uncollected.
    team_factory = TeamFactory(
        repository,
        runtime,
        resolve_team_selector,
        lambda: runtime_support,
    )
    observed_model = ObservedPort(
        model, observer, "model", mode="mock" if model.simulated else "real"
    )
    observed_retrieval = ObservedPort(
        retrieval, observer, "retrieval", mode="mock" if retrieval.simulated else "local"
    )
    observed_policy = ObservedPort(
        policy,
        observer,
        "policy",
        mode="local",
        provider_kind="reference_http" if settings.policy_backend == "http" else "builtin",
    )

    if settings.enable_judge and settings.judge_provider == "nvidia":
        # P1-006A: explicit opt-in only; no mock fallback. Each call still needs the
        # synthetic/public Judge gate; ENABLE_JUDGE and the key are selection, not permission.
        from rfa_mas.adapters.nvidia_judge import NvidiaJudge, SyntheticPublicJudgeGate

        judge_config = _judge_config(settings)
        judge = NvidiaJudge(
            judge_config,
            SyntheticPublicJudgeGate(
                endpoint=judge_config.endpoint,
                model=judge_config.model,
                max_output_tokens=settings.nvidia_max_output_tokens,
                budget_seconds=settings.tool_timeout_seconds,
            ),
            transport=model_transport,
        )
    else:
        judge = MockJudge()
    observed_runtime = ObservedPort(
        runtime,
        observer,
        "runtime",
        mode="local",
        provider_kind="reference_http" if settings.runtime_backend == "http" else "builtin",
    )
    # P0-020: roles use only the allowlisted local READ computations; the configured
    # external ToolPort (mock/HTTP) is not granted to team roles. P1-003 adds only the
    # official nemo-retriever Skill tool, and only when RETRIEVER_BACKEND=nemo_cli.
    team_tools, external_search = _team_tools(settings, observer)
    team_runner = TeamRunner(
        repository=repository,
        factory=team_factory,
        runtime=observed_runtime,
        retrieval=observed_retrieval,
        tools=team_tools,
        policy_version=lambda: policy.policy_version,
        external_search=external_search,
        # P1-008K: owner-consented LLM synthesis of the supervisor summary (real model only).
        model=model if settings.external_egress_effective else None,
    )

    # P0-025: owner event feed; present/validate are read at call time from the service.
    events = EventFeed(
        repository,
        validate_draft=lambda: service._dependencies.validate_resume,
        present_result=lambda result, principal: service.present_result(result, principal),
    )

    if isinstance(runtime, LocalRuntime):
        staged_context = StagedContextBoundary(
            # P0-025: the same boundary also records Run-linked stage outcomes/source refs.
            _staged_context(
                repository, policy, settings, observer, on_context=events.record_context
            )
        )
        # P1-005B: owner feedback memory. Markers only narrow; style is advisory model input.
        feedback = FeedbackService(repository)
        runtime.register(
            "domain_task",
            build_domain_task_handler(
                DomainGraphDependencies(
                    model=observed_model,
                    retrieval=observed_retrieval,
                    policy=observed_policy,
                    context=lambda principal, work, request: staged_context.load(
                        principal, work, request
                    ),
                    model_endpoint="local" if settings.model_provider == "mock" else "cloud",
                    private_egress=settings.external_egress_effective,
                    disclosure_markers=feedback.disclosure_markers,
                    style_guidance=feedback.style_guidance,
                )
            ),
        )
        runtime.register(TEAM_ROLE_TASK, team_runner.handler())
    else:
        staged_context = None

    adapters = tuple(
        AdapterInfo(port=port, adapter=adapter.adapter_name, simulated=adapter.simulated)
        for port, adapter in (
            ("model", model),
            ("retrieval", retrieval),
            ("response", response),
            ("runtime", runtime),
            ("policy", policy),
            ("trace", trace),
        )
    )
    knowledge = KnowledgeService(repository, policy)
    service = WorkService(
        repository=repository,
        trace=trace,
        supervisor_dependencies=SupervisorDependencies(
            runtime=observed_runtime,
            response=ObservedPort(
                response,
                observer,
                "approval",
                mode="local" if settings.response_backend == "http" else "mock",
                provider_kind="reference_http"
                if settings.response_backend == "http"
                else "builtin",
            ),
            max_graph_steps=settings.max_graph_steps,
            max_tool_calls=settings.max_tool_calls,
            validate_resume=ResumePolicy(repository, observed_policy),
            team_runner=team_runner,
            policy_version=lambda: policy.policy_version,
            accumulator=knowledge.accumulator,
        ),
        adapters=adapters,
        guard_thread=checkpoints.guard,
        observations=observer,
    )

    # P1-005A/P1-008: mock backend -> in-process MockPublisher; http backend -> the
    # Response service's publication endpoint (P1-008C local stand-in contract, mode=mock
    # receipts). Never a silent mock fallback for a selected http backend.
    async def installation_owner() -> str | None:
        return (await repository.local_principal()).user_id

    if response_client is None:
        publisher = MockPublisher()
    else:
        publisher = PublicationHttpAdapter(response_client, expected_approver=installation_owner)
    drafts = DraftLifecycle(
        repository=repository,
        dependencies=lambda: service._dependencies,
        publisher=publisher,
    )
    return Container(
        settings=settings,
        repository=repository,
        service=service,
        knowledge=knowledge,
        model=model,
        retrieval=retrieval,
        response=response,
        tool=tool,
        runtime=runtime,
        policy=policy,
        trace=trace,
        judge=judge,
        adapters=adapters,
        checkpoints=checkpoints,
        team_factory=team_factory,
        team_runner=team_runner,
        drafts=drafts,
        context=staged_context,
        schedules=ScheduleService(
            repository, ApschedulerTriggers(), default_timezone=settings.default_timezone
        ),
        http_clients=clients,
        readiness_probes=probes,
        events=events,
    )


def build_scheduler_runner(
    settings: Settings,
    container: Container,
    *,
    clock: ManualClock | None = None,
    sync_interval_seconds: float | None = 30.0,
) -> SchedulerRunner:
    """Composition for the dedicated `rfa scheduler` process only (never the API lifespan).

    Jobs call only existing internal services with the owner re-resolved at every fire.
    No publisher, response/tool port or team runner is reachable from here.
    """
    if not settings.scheduler_enabled:
        raise ConfigurationError(["SCHEDULER_ENABLED"])
    now = clock.now if clock is not None else (lambda: datetime.now(UTC))
    repository = container.repository

    async def resolve_owner(owner_id: str) -> TrustedPrincipal | None:
        # Single-installation identity, re-read for every fire (never cached in the job).
        principal = await repository.local_principal()
        return principal if principal.user_id == owner_id else None

    accumulator = container.knowledge.accumulator
    executor = ScheduleExecutor(
        repository,
        resolve_principal=resolve_owner,
        candidates=CandidateService(repository, accumulator, clock=now),
        accumulator=accumulator,
        clock=now,
        # P0-024: every fire is mirrored into the P0-021 durable effect ledger.
        effects=LedgerScheduledEffectHook(repository),
    )
    return SchedulerRunner(
        jobstore_path=settings.scheduler_jobstore_path,
        repository=repository,
        executor=executor,
        misfire_policy=settings.scheduler_misfire_policy,
        sync_interval_seconds=sync_interval_seconds,
        clock=clock,
        now=now,
    )
