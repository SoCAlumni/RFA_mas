from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from rfa_mas.adapters.checkpoints import SqliteCheckpoints

from rfa_mas.adapters.http import (
    PolicyHttpAdapter,
    ReferenceHttpClient,
    ResponseHttpAdapter,
    RuntimeHttpAdapter,
    ToolHttpAdapter,
    require_loopback_reference_url,
)
from rfa_mas.adapters.local import LocalJsonlTrace, LocalPolicy, LocalRuntime, SqliteWorkRepository
from rfa_mas.adapters.mock import MockJudge, MockModel, MockResponse, MockRetrieval, MockTool
from rfa_mas.application.graphs import (
    DomainGraphDependencies,
    SupervisorDependencies,
    build_domain_task_handler,
)
from rfa_mas.application.service import WorkService
from rfa_mas.application.resume_policy import ResumePolicy
from rfa_mas.contracts import AdapterInfo, KnowledgeDocument
from rfa_mas.errors import BackendNotImplementedError
from rfa_mas.security import SecretRedactor, configure_logging
from rfa_mas.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    http_clients: list[httpx.AsyncClient] = field(default_factory=list)
    ready: bool = False

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
) -> ReferenceHttpClient:
    require_loopback_reference_url(base_url, setting_name=endpoint_name)
    client = httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(settings.http_timeout_seconds),
    )
    reference_client = ReferenceHttpClient(
        client,
        token=token,
        max_read_retries=settings.max_read_retries,
        endpoint_name=endpoint_name,
    )
    clients.append(client)
    return reference_client


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    settings.ensure_ready()
    if reserved_features := settings.selected_reserved_features():
        raise BackendNotImplementedError(reserved_features[0])
    redactor = SecretRedactor(settings.secret_values())
    configure_logging(settings.log_level, redactor)
    repository = SqliteWorkRepository(settings.database_path)
    checkpoints = SqliteCheckpoints(settings.resolved_checkpoint_path)
    clients: list[httpx.AsyncClient] = []

    if settings.model_provider != "mock":
        raise BackendNotImplementedError(f"model:{settings.model_provider}")
    model = MockModel()

    if settings.retriever_backend != "mock":
        raise BackendNotImplementedError(f"retriever:{settings.retriever_backend}")

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
            )
        )

    retrieval = MockRetrieval(repository, policy_version=policy.policy_version)

    if settings.response_backend == "mock":
        response = MockResponse()
    else:
        response = ResponseHttpAdapter(
            _http_port(
                base_url=settings.response_base_url or "",
                token=settings.response_api_token,
                settings=settings,
                clients=clients,
                endpoint_name="RESPONSE_BASE_URL",
            )
        )

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
            )
        )

    if settings.trace_backend != "local":
        raise BackendNotImplementedError(f"trace:{settings.trace_backend}")
    trace = LocalJsonlTrace(settings.trace_dir, redactor)

    if settings.enable_judge and settings.judge_provider != "mock":
        raise BackendNotImplementedError(f"judge:{settings.judge_provider}")
    judge = MockJudge()

    if isinstance(runtime, LocalRuntime):
        runtime.register(
            "domain_task",
            build_domain_task_handler(
                DomainGraphDependencies(model=model, retrieval=retrieval, policy=policy)
            ),
        )

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
    service = WorkService(
        repository=repository,
        trace=trace,
        supervisor_dependencies=SupervisorDependencies(
            runtime=runtime,
            response=response,
            max_graph_steps=settings.max_graph_steps,
            max_tool_calls=settings.max_tool_calls,
            validate_resume=ResumePolicy(repository, policy),
        ),
        adapters=adapters,
        guard_thread=checkpoints.guard,
    )
    return Container(
        settings=settings,
        repository=repository,
        service=service,
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
        http_clients=clients,
    )
