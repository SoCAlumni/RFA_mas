from __future__ import annotations

import json
from dataclasses import dataclass, field
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
from rfa_mas.application.drafts import DraftLifecycle
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
from rfa_mas.application.service import WorkService
from rfa_mas.application.team_selector import APPROVED_PINS, TeamSelector, TemplateRegistry
from rfa_mas.application.teams import RuntimeLifecycleSupport, TeamFactory
from rfa_mas.application.workers import TEAM_ROLE_TASK, TeamRunner
from rfa_mas.contracts import (
    AdapterInfo,
    DomainId,
    ExecutionMode,
    KnowledgeDocument,
    TeamBudget,
    TrustedPrincipal,
)
from rfa_mas.errors import BackendNotImplementedError, ConfigurationError
from rfa_mas.security import SecretRedactor, configure_logging
from rfa_mas.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    for name, mode, url in (
        ("RESPONSE_BASE_URL", settings.response_backend, settings.response_base_url),
        ("TOOL_BASE_URL", settings.tool_backend, settings.tool_base_url),
        ("RUNTIME_BASE_URL", settings.runtime_backend, settings.runtime_base_url),
        ("POLICY_BASE_URL", settings.policy_backend, settings.policy_base_url),
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
        ("model", settings.model_provider, "mock"),
        ("trace", settings.trace_backend, "local"),
        ("judge", settings.judge_provider if settings.enable_judge else "mock", "mock"),
    ):
        if selected != default:
            reserved.append(f"{port}:{selected}")
    if settings.retriever_backend not in {"local", "mock"}:
        reserved.append(f"retriever:{settings.retriever_backend}")
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
    http_clients: list[httpx.AsyncClient] = field(default_factory=list)
    ready: bool = False

    def context_reader(self, bound: BoundAccess) -> BoundContextReader:
        """Internal trusted composition, not a request-body factory or role grant."""
        return BoundContextReader(self.repository,self.policy,bound,
                                  issuer_supported=self.settings.policy_backend == "local")

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
        self.ready = False


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


def _http_port(
    *,
    base_url: str,
    token: object,
    settings: Settings,
    clients: list[httpx.AsyncClient],
    endpoint_name: str,
    transport: httpx.AsyncBaseTransport | None = None,
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
    return reference_client


def _staged_context(repository, policy, settings: Settings, observer: Observations):
    """Trusted factory (P1-005): staged L0/L1/L2 context for owner/public local targets.

    Returns None when the target/endpoint is outside the bound reader's supported scope,
    so the graph uses the ACL-bound retrieval path and the share/egress screen instead.
    """
    from time import perf_counter

    from rfa_mas.application.context import ContextLoader
    from rfa_mas.contracts import Audience, ContextRequest, DraftTarget, EvidenceBundle, EvidenceItem

    async def load(principal, work, request):
        target = work.target.audience
        if target not in {Audience.OWNER, Audience.PUBLIC} or settings.policy_backend != "local":
            return None
        bound_target = DraftTarget(audience=target)
        bound = BoundAccess(lambda: principal, request.agent_id, "supervisor",
                            request.domain_id, bound_target, "mock-model")
        loader = ContextLoader(repository, BoundContextReader(
            repository, policy, bound, issuer_supported=True))
        context_request = ContextRequest.model_validate(
            request.model_dump(exclude={"schema_version"})
            | {"goal": work.query, "role": "supervisor", "target": bound_target,
               "endpoint_id": "mock-model"})
        await observer.record("retrieval", "started", mode="local")
        start = perf_counter()
        try:
            loaded = await loader.load(context_request)
        except BaseException:
            await observer.record("retrieval", "failed", mode="local", reason="provider_error",
                                  duration_ms=(perf_counter() - start) * 1000, transport="raised")
            raise
        items = tuple(
            EvidenceItem(source_id=i.source_id, source_revision=i.source_revision,
                         location=i.location, audience=i.audience, excerpt=i.excerpt,
                         content_hash=i.content_hash, policy_version=i.policy_version)
            for i in loaded.bundle.items
        )
        await observer.record("retrieval", "succeeded", mode="local",
                              duration_ms=(perf_counter() - start) * 1000,
                              sources=tuple((i.source_id, i.source_revision) for i in items),
                              transport="returned")
        evidence = EvidenceBundle(
            request_id=request.request_id, trace_id=request.trace_id, run_id=request.run_id,
            agent_id=request.agent_id, domain_id=request.domain_id, items=items,
            insufficient=loaded.insufficient, policy_version=policy.policy_version,
            simulated=False, adapter="staged-context-v1",
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
) -> Container:
    settings = settings or Settings()
    inspect_configuration(settings).require_available()
    redactor = SecretRedactor(settings.secret_values())
    configure_logging(settings.log_level, redactor)
    repository = SqliteWorkRepository(settings.database_path, project_resolver=project_resolver)
    checkpoints = SqliteCheckpoints(settings.resolved_checkpoint_path)
    clients: list[httpx.AsyncClient] = []

    model = MockModel()

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
            )
        )

    if settings.retriever_backend == "local":
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
            )
        )

    trace = LocalJsonlTrace(
        settings.trace_dir,
        redactor,
        repository=repository,
        retention_days=settings.trace_retention_days,
    )
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
    observed_model = ObservedPort(model, observer, "model", mode="mock")
    observed_retrieval = ObservedPort(retrieval, observer, "retrieval",
                                     mode="mock" if retrieval.simulated else "local")
    observed_policy = ObservedPort(
        policy,
        observer,
        "policy",
        mode="local",
        provider_kind="reference_http" if settings.policy_backend == "http" else "builtin",
    )

    judge = MockJudge()
    observed_runtime = ObservedPort(
        runtime,
        observer,
        "runtime",
        mode="local",
        provider_kind="reference_http" if settings.runtime_backend == "http" else "builtin",
    )
    # P0-020: roles use only the allowlisted local READ computations; the configured
    # external ToolPort (mock/HTTP) is not granted to team roles.
    team_runner = TeamRunner(
        repository=repository,
        factory=team_factory,
        runtime=observed_runtime,
        retrieval=observed_retrieval,
        tools=ObservedPort(LocalAnalysisTools(), observer, "tool", mode="local"),
        policy_version=lambda: policy.policy_version,
    )

    if isinstance(runtime, LocalRuntime):
        staged_context = StagedContextBoundary(
            _staged_context(repository, policy, settings, observer)
        )
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
        http_clients=clients,
    )
