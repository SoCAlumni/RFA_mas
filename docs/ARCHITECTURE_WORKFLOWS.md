# RFA MAS 시스템 아키텍처와 시나리오별 Agent Workflow

기준: main `683172d` (2026-09-27). 이 문서는 현재 구현된 코드 기준의 사실만 기술한다.
mock/local 구현과 실제 외부 연동을 구분하며, 미검증 경로를 완료로 표기하지 않는다.

문서 구성: 1장 전체 아키텍처 → 2장 팀원 모듈 통합 아키텍처(민섭 core · 승희 · 다영) →
3장 기술 스택 → 4장 Agent 인벤토리 → 5장 팀 에이전트 생성 → 6장 라우팅 규칙 →
7장 시나리오별 workflow → 8장 LLMOps → 9장 실제/mock 경계.

## 1. 전체 아키텍처

의존 방향은 `API → application/graph → port`로 고정이며, adapter 주입은 `bootstrap.py`에서만 한다.
graph 노드 내부에서 mock/real adapter를 분기하지 않는다.

```mermaid
flowchart TB
    subgraph ENTRY["진입점 (독립 프로세스)"]
        UI["PoC 채팅 UI/서버 :8780<br/>python -m rfa_mas.poc"]
        API["Core FastAPI :8000<br/>rfa api (/v1/*)"]
        KF["Knowledge Facade :8791<br/>승희 RFA_module 계약"]
        INBOX["Inbox reference :8793<br/>다영 결재 UI 계약"]
        SCHED["rfa scheduler<br/>전용 단일 프로세스"]
    end

    subgraph APP["Application 계층 (src/rfa_mas/application)"]
        CHATP["ChatPort + LocalChatRouter<br/>채팅 intent/담당 라우팅"]
        SVC["AssistantService<br/>결정적 INTENT_RULES"]
        SUPG["Supervisor Graph (LangGraph)<br/>route→delegate/delegate_team→review"]
        DOMG["Domain Graph (LangGraph)<br/>authorize→retrieve→generate"]
        TEAMF["TeamSelector + TeamFactory<br/>팀 선택·예약·provisioning"]
        TEAMR["TeamRunner<br/>역할 순차 실행 + 예산 강제"]
        KACC["KnowledgeAccumulator<br/>파생 지식 축적"]
        CAND["Candidates<br/>할 일 후보 발굴"]
        RESUME["ResumePolicy + EffectLedger<br/>승인 version/hash 검증"]
        OBS["Observations + ObservedPort<br/>경계 관측 원장"]
    end

    subgraph PORTS["Ports (인터페이스)"]
        MP["ModelPort"]
        RP["RetrievalPort"]
        RTP["RuntimePort"]
        PP["PolicyPort"]
        RSP["ResponsePort"]
        TLP["ToolPort"]
        TRP["TracePort"]
    end

    subgraph ADPT["Adapters (bootstrap.py 주입)"]
        AMOCK["mock.py<br/>결정적 mock 전부"]
        ALOCAL["local.py / retrieval.py<br/>SQLite·lexical index"]
        ANV["nvidia.py / nvidia_judge.py<br/>integrate.api.nvidia.com"]
        ANEMO["nemo_retriever.py<br/>NeMo Retriever cli/service"]
        AHTTP["http.py<br/>팀원 서비스 HTTP"]
        ALF["langfuse.py<br/>trace export"]
        ASCH["scheduler.py<br/>APScheduler 3.11.3"]
    end

    subgraph STORE["로컬 저장소"]
        DB[("rfa.db SQLite<br/>KB·Task·Team·승인·관측 원장")]
        CKPT[("LangGraph checkpoint")]
        IDX[("retriever index")]
        TRC[("traces/ JSONL")]
        CHDB[("chat history.db")]
    end

    UI --> CHATP --> SVC
    API --> SVC
    SCHED --> SVC
    SVC --> SUPG
    SUPG -->|"단일 domain 작업"| DOMG
    SUPG -->|"명시적 팀 실행"| TEAMF --> TEAMR
    SUPG --> RESUME
    TEAMR --> KACC --> CAND
    DOMG --> MP & RP & PP
    TEAMF --> RTP
    TEAMR --> RTP & RP & TLP
    SUPG --> RSP
    OBS --> TRP
    MP & RP & PP & RTP & RSP -.->|"ObservedPort 감싸기"| OBS
    MP -.-> AMOCK & ANV
    RP -.-> ALOCAL & AMOCK & ANEMO
    RTP -.-> ALOCAL & AHTTP
    PP -.-> ALOCAL & AHTTP
    RSP -.-> AMOCK & AHTTP
    TLP -.-> AMOCK & AHTTP
    TRP -.-> ALOCAL & ALF
    ALOCAL --> DB & IDX
    SUPG --> CKPT
    TRP --> TRC
    CHATP --> CHDB
    KF --> APP
    INBOX -.->|"계약 표본 (in-memory)"| INBOX
```

## 2. 팀원 모듈 통합 아키텍처 (민섭 core · 승희 · 다영)

세 사람의 산출물을 **교체 가능한 모듈**로 보고, 각 모듈이 어느 port/계약으로 core에 붙는지 정리한다.
원칙은 하나다: 팀원 모듈이 바뀌어도 core graph·port·canonical DTO는 그대로 두고 adapter/DTO mapper와
설정(`*_BACKEND`, `*_BASE_URL`, `*_API_TOKEN`)만 바꾼다. 팀원 서비스가 없을 때는 같은 자리를
이 저장소의 stand-in이 채우며, stand-in 성공은 팀원 서비스 호환 증거가 아니다.

```mermaid
flowchart LR
    subgraph CORE["민섭 — rfa_mas core (이 저장소)"]
        direction TB
        C_API["Core API :8000<br/>/v1/sessions·work·runs·inbox"]
        C_APP["application/graph<br/>Supervisor·Domain·Team"]
        C_PORTS["Ports<br/>Model·Retrieval·Policy·Runtime·Response·Tool·Trace"]
        C_KB[("KB / Task / Team / 승인 / 관측 원장")]
        C_API --> C_APP --> C_PORTS
        C_APP --> C_KB
    end

    subgraph SH["승희 — RFA_module (외부게시 MCP Agent)"]
        direction TB
        SH_WF["workflow/rfa_workflow<br/>LangGraph 공개 대응 그래프"]
        SH_REV["services/review :8790<br/>결재 문서 상태기계"]
        SH_KN["services/knowledge_stub :8791<br/>(facade로 교체 대상)"]
        SH_MCP["services/mcp_channels :8792<br/>GitHub MCP 읽기 도구"]
        SH_WF --> SH_REV & SH_KN & SH_MCP
    end

    subgraph DY["다영 — RequestForApproval (NemoClaw/OpenShell 운영)"]
        direction TB
        DY_GW["OpenShell gateway :8080<br/>dashboard :18789"]
        DY_POL["policies/rfa-host-services.yaml<br/>REST allowlist · approve/reject deny"]
        DY_AG["agents/rfa.yaml<br/>public-desk 허용 도구 3개"]
        DY_MOD["modules.yaml<br/>모듈 repo·ref·port·실행 명령"]
        DY_INF["inference.local / proxy 10.200.0.1:3128"]
        DY_GW --> DY_POL & DY_AG
    end

    subgraph GLUE["이 저장소의 연결 모듈 (교체 지점)"]
        direction TB
        G_KF["knowledge_facade :8791<br/>승희 knowledge 계약을 core 위에서 제공"]
        G_MAP["adapters/rfa_module.py<br/>Review → RequestDetail 매퍼"]
        G_INBOX["inbox reference :8793<br/>결재 UI 계약 표본"]
        G_LR["reference/local_response<br/>검토·게시·READ tool stand-in"]
        G_LRT["reference/local_runtime<br/>TeamSpec lifecycle stand-in"]
        G_UI["ui/ 동일 출처 UI<br/>core API client"]
    end

    SH_WF -->|"GET /tasks · POST /tasks/id/ask"| G_KF --> C_APP
    SH_REV -.->|"ResponsePort http (P1-008A gate)"| C_PORTS
    SH_REV -->|"Review JSON"| G_MAP --> G_INBOX
    G_LR -.->|"ResponsePort/ToolPort http (loopback)"| C_PORTS
    G_LRT -.->|"RuntimePort http (loopback, prepare/cleanup만)"| C_PORTS
    DY_GW -.->|"RuntimePort openshell (not_run)"| C_PORTS
    DY_POL -->|"rfa_knowledge 규칙이 facade 2 route 허용"| G_KF
    DY_MOD -->|"knowledge 실행 명령 = rfa knowledge-facade"| G_KF
    G_UI --> C_API
    G_UI --> G_LR
```

### 2.1 모듈 ↔ port ↔ 현재 채워진 구현

| 책임 모듈 | 소유자 | core 접점 | 지금 자리를 채우는 것 | 실제 연동 상태 |
|---|---|---|---|---|
| 지식 제공 (실무대장) | 민섭 | `knowledge_facade` (승희 `knowledge.openapi.yaml` 동일 계약) | `rfa knowledge-facade` — PolicyPort→audience 제한 검색→share_egress_filter→ModelPort→canary 치환 | offline 계약 검증. RFA_module 워크플로와의 실제 연동 실행은 not_run |
| 검토·승인 원본 | 승희 `services/review` | ResponsePort (`RESPONSE_BACKEND=http`) | `reference/local_response` stand-in (별도 SQLite, 수동 승인, mock receipt) | 승희 review 실 어댑터 교체는 P1-008A gate. 매퍼(`adapters/rfa_module.py`)만 존재 |
| 게시 (외부 채널) | 승희 워크플로 + MCP channels | ResponsePort 게시 경로 (`PublicationHttpAdapter`) | stand-in의 `POST /v1/local/publications` 합성 receipt | 실게시 없음. P0는 `ALLOW_EXTERNAL_WRITES=true`여도 외부 write 금지 |
| Tool 실행 | 승희 (MCP) | ToolPort (`TOOL_BACKEND=http`) | stand-in `POST /v1/tools/execute` — 무부작용 합성 READ allowlist | MCP gateway 실연동 없음. graph 노드는 MCP SDK를 직접 호출하지 않음 |
| Runtime / sandbox | 다영 OpenShell | RuntimePort (`RUNTIME_BACKEND=http`) | core `LocalRuntime` (sandbox 아님) · `reference/local_runtime` (TeamSpec prepare/cleanup round-trip) | http runtime의 팀 **실행**은 명시 unsupported(prepare/cleanup만). OpenShell 실행 증거 P1-007C, 역할별 identity P1-007B(blocked) |
| 권한 정책 | 미확정 (다영 policy와 core local 정책 병행) | PolicyPort (`POLICY_BACKEND=local`/http) | core local 결정 정책 | http 소유권 미확정 |
| 결재 인박스 UI | 다영 | `/v1/inbox` 계약 (`docs/api/inbox.openapi.yaml`) | `inbox reference :8793` in-memory 표본. 각 operation의 `x-rfa-authority`가 실제 원본(승희 review/다영 runtime/민섭 core) 표시 | 표본. 실제 UI 연결 시 core API·review 원본으로 교체 |
| 로컬 UI | 민섭 | core API client | `ui/` 동일 출처 plain DOM (core API + 검토 stand-in 고정 upstream) | 팀원 UI로 전체 교체 가능. 사용자 지정 URL proxy 없음 |

### 2.2 요청이 모듈을 가로지르는 경로

승희 워크플로가 공개 채널 질문에 답하는 경우(승희가 채널·초안·검열·결재·게시 소유, 우리는 지식만 제공):

```mermaid
sequenceDiagram
    participant CH as 외부 채널 (GitHub 등)
    participant WF as 승희 rfa_workflow
    participant KF as knowledge_facade :8791 (민섭)
    participant CORE as core application
    participant REV as 승희 review :8790
    participant H as 사람 결재 (다영 인박스 UI)
    CH->>WF: mention (mcp_channels 읽기)
    WF->>KF: GET /tasks (audience가 읽을 수 있는 domain만)
    WF->>KF: POST /tasks/{id}/ask
    KF->>CORE: PolicyPort(retrieve, target=facade audience) → RetrievalPort → egress filter → ModelPort
    CORE-->>KF: 공유 가능한 근거만 / 없으면 answer=""
    KF-->>WF: KnowledgeResult(answer, sources, confidence)
    WF->>WF: draft → scan(기밀 검열) → reviewed
    WF->>REV: 결재 문서 등록
    REV-->>H: needs_approval (Review → RequestDetail 매퍼)
    H->>REV: approve/reject (loopback 전용, 에이전트 route allowlist에 없음)
    REV-->>WF: approved → posted (clearance HMAC: review_id·target·sha256(body)·exp)
```

다영 sandbox 정책(`rfa_knowledge`)은 facade의 두 route만 허용하므로 추가 정책이 필요 없다.
core API(8000)는 아직 sandbox 허용 목록에 없다. 필요하면 agent 허용 4 route(`/healthz`,
`POST /v1/sessions`, `POST /v1/sessions/{id}/work`, `GET /v1/runs/{id}`)만 추가한다.

### 2.3 승희 review 상태 ↔ core/인박스 상태 대응

| RFA_module Review | rfa_mas | 인박스 |
|---|---|---|
| `opened → knowledge_ready → drafted → scanned` | `running` (`in_progress`) | 진행 단계 라벨 |
| `reviewed` | `waiting_approval` | `needs_approval` |
| `approved → posted` | `completed` + `PublicationReceipt(succeeded)` | `decided` |
| `rejected` | `failed(rejected)` | `declined` |
| `needs_human` | — | `needs_human` |
| clearance HMAC | `DraftBinding`(payload_hash·target·policy) | 본문/대상 변경 시 승인 무효 (양쪽 동일) |

### 2.4 교체 순서 (요약)

1. 팀원 서비스가 같은 route·envelope·오류 의미면 설정만 바꾼다: `*_BACKEND=http`, `*_BASE_URL`, `*_API_TOKEN`.
   현재 adapter와 UI upstream은 loopback URL만 허용하므로 원격 서비스는 loopback gate를 실제 credential·egress
   경계로 바꾸는 작업(P1-008A/B)이 먼저다.
2. `uv run rfa doctor`로 변수 이름의 configured/missing만 확인하고 `/readyz`로 각 http backend의 `/healthz` 도달을 본다.
3. 계약 test(`tests/test_http_contract.py`, `test_consumer_safety.py`, `test_local_response.py`, `test_local_runtime.py`,
   `test_knowledge_facade.py`)와 `scripts/contract_baseline.py check`/`check-extended`를 실행한다.
4. route/envelope가 다르면 `adapters/http.py`의 adapter와 DTO mapper만 수정한다. graph·port·canonical DTO에
   팀원 전용 field/URL/인증을 넣지 않는다.

세부 절차와 timeout/idempotency 의미는 [INTEGRATION.md](INTEGRATION.md), stand-in 안전 경계는
[LOCAL_MODULES.md](LOCAL_MODULES.md), 팀원 레포 커밋 기준 대응표는 [TEAM_ALIGNMENT.md](TEAM_ALIGNMENT.md)에 있다.

## 3. 기술 스택과 backend 스위치

모든 외부 의존은 port 뒤에 있고, `settings.py`의 backend 값으로 adapter가 결정된다.
P0 기본값은 전부 local/mock이며 key·GPU·Docker 없이 완주한다.

| Port | backend 선택지 (기본값 굵게) | 실제 연동 대상 | 실검증 상태 |
|---|---|---|---|
| ModelPort | **mock** / nvidia | NVIDIA integrate API (Nemotron 계열, `NVIDIA_MODEL`) | P1-002에서 실호출 검증됨. 팀 role에는 미연결 |
| RetrievalPort | **local**(lexical index) / mock / nemo_cli / nemo_service | NeMo Retriever | nemo 경로는 별도 gate, 기본은 local |
| RuntimePort | **local**(LocalRuntime stand-in) / http | 다영 OpenShell runtime | http는 prepare/cleanup만, 팀 실행 unsupported. local은 sandbox 아님 |
| PolicyPort | **local** / http | 다영 권한 서비스 | 소유권 미확정, local 결정 정책 |
| ResponsePort | **mock** / http | 승희 게시/receipt 서비스 | mock receipt. 실게시는 P1-008A 계열 gate |
| ToolPort | **mock** / http | 승희 Tool 서비스 | 팀 role tool은 결정적 mock |
| TracePort | **local**(JSONL) / langfuse | Langfuse (loopback 한정 export flag) | P1-006C/F live 검증 (8장) |
| Judge | **mock** / nvidia (`ENABLE_JUDGE` 기본 false) | NVIDIA 모델 judge | 평가 전용 opt-in. nvidia 선택 시 mock fallback 없음 |
| Scheduler | **apscheduler** (3.11.3 고정) | — | 전용 프로세스, API worker에서 시작 금지 |

공통 프레임워크: FastAPI(HTTP 계약), LangGraph(StateGraph + interrupt + SQLite checkpointer),
Pydantic(공유 DTO/contracts, 공개 계약은 생성 OpenAPI), SQLite(KB·상태·승인·chat·관측 원장), Ruff/pytest.
평가/관측 보조: NVIDIA Agent Toolkit(NAT) 1.8 eval(spike, 8.6절), Langfuse 4.x OSS(opt-in export).

## 4. Agent 인벤토리

"에이전트"는 상주 데몬이 아니라 **요청 시 실행되는 코드 단위**다. LLM 호출 지점은
Domain Graph의 `generate` 노드(ModelPort)와 knowledge facade의 생성 단계이며, 나머지 라우팅·역할·검토는 결정적 로직이다.

| Agent | 위치 | 구현 기술 | 호출 조건 |
|---|---|---|---|
| 비서 (Assistant) | `application/service.py` | 결정적 INTENT_RULES (키워드, first-match) | 모든 WorkRequest의 첫 관문 |
| 채팅 라우터 | `poc/routing.py` LocalChatRouter | lexical 주제어 매칭, TeamCatalogPort read-only | PoC 채팅 입력마다 |
| Supervisor | `application/graphs/supervisor.py` | LangGraph StateGraph + interrupt | route 결과가 delegate/delegate_team일 때 |
| Domain worker | `application/graphs/domain.py` | authorize(PolicyPort)→retrieve(RetrievalPort)→generate(ModelPort) | 단일 domain 질의/초안 |
| TeamSelector | `application/team_selector.py` | 승인 템플릿 registry + pin, fail-closed 규칙 선택 | 팀 생성 요청마다 (매 호출 재해석) |
| TeamFactory | `application/teams.py` | Task/Team 예약, RuntimePort prepare/cleanup, 응답 결합 검증 | 선택 성공 후 |
| paper_scout | `application/workers.py` | RetrievalPort 검색 + 논문 정규식 필터 | benchmark 팀 1단계 |
| experiment_runner | 〃 | `benchmark_log_parse` tool (합성 로그 파싱) | benchmark 팀 2단계 |
| result_analyst (검증) | 〃 | `metric_compare` tool, tool 예산 강제 | benchmark 팀 3단계 |
| source_scout | 〃 | RetrievalPort 검색 | research 팀 1단계 |
| evidence_reviewer (검증) | 〃 | TENTATIVE_TERMS 키워드로 cited/tentative 분류 | research 팀 2단계 |
| 팀 supervisor | 〃 | 역할 결과 취합 요약 (결정적) | 모든 팀 마지막 단계 |
| KnowledgeAccumulator | `application/knowledge.py` | 팀 결과→파생 지식, 미검증 표기 보존 | 팀 실행 후 Supervisor gate |
| Judge | `adapters/nvidia_judge.py` | NVIDIA 모델 (선택) | 평가 전용. 권한 gate 상쇄 불가 |

통신 규칙: 외부 요청·팀 간·팀 내 위임/결과는 전부 Supervisor 경유(`supervisor_only`).
worker 간 직접 통신 금지, P0 Debate 금지.

## 5. 팀 에이전트 생성 (Team provisioning)

팀은 사용자 문장이나 LLM이 아니라 **서버가 승인한 템플릿과 인증된 principal**로부터 만들어진다.
사용자 입력이 제공하는 것은 `TeamExecutionRequest(goal, outputs, requested_pattern)` 세 값뿐이며
capability·identity·grant 주장은 들어갈 자리가 없다. 템플릿 선택, 역할 capability, 예산은 모두
`TeamSelector`/`TeamFactory`가 서버 쪽 값으로 결정한다.

### 5.1 생성 파이프라인

```mermaid
flowchart TB
    IN["TeamExecutionRequest<br/>goal · outputs · requested_pattern"] --> SVC["AssistantService<br/>intent=task_run + task_candidate"]
    SVC --> SEL

    subgraph SEL["TeamSelector.select (순수·fail-closed, 매 호출 재해석)"]
        direction TB
        S0["입력 strict 재검증<br/>(model_copy 우회·모호한 숫자 거절)"]
        S1{"principal 인증?"} -->|no| D1["denied: authentication_required"]
        S1 -->|yes| S2{"(user, domain) grant 존재?"}
        S2 -->|no| D2["denied: domain_permission_missing"]
        S2 -->|yes| S3{"요청 예산 ≤ 서버 ceiling?"}
        S3 -->|no| D3["denied: budget_exceeds_authority"]
        S3 -->|yes| S4["registry 순회<br/>fixtures/teams/templates.json + APPROVED_PINS"]
        S4 --> S5["후보별 검사: goal_terms 포함 · outputs ⊆ · pattern 일치<br/>required_capabilities ⊆ grant ∧ ⊆ available · runtime_kind ∈ available<br/>effective=min(요청,템플릿) ≥ minimum_budget"]
        S5 -->|"적격 0개"| D4["unavailable: no_eligible_team (+ 후보별 거절 사유)"]
        S5 -->|"적격 ≥1"| S6["결정적 순위: 매칭 용어 수 desc → template_id → version"]
        S0 --> S1
    end

    S6 --> FAC

    subgraph FAC["TeamFactory.ensure"]
        direction TB
        F1["runtime lifecycle 지원 확인<br/>(local만 supported)"] --> F2["task_id 있으면 기존 Task 재사용:<br/>domain·goal·status=active 일치 필수"]
        F2 --> F3["TeamSpec 조립<br/>역할별 AgentSpec: agent_id=team:role<br/>memory_namespace=domains/D/tasks/T/teams/M/role<br/>instructions_ref=approved:digest:role"]
        F3 --> F4["repository.reserve_team<br/>Idempotency-Key + 요청 fingerprint<br/>Task당 활성 팀 1개 durable slot"]
        F4 -->|"재전송/경쟁"| F5["원본 식별자 반환 후<br/>현재 선택과 저장된 binding 대조"]
        F4 -->|"신규"| F6["RuntimePort.prepare(spec, operation_key)<br/>timeout=execution_budget.timeout_seconds"]
        F6 --> F7["응답 결합 검증 _bound<br/>spec·mode 동일 · 멤버 상태 전원 · failed 일관성"]
    end

    F7 -->|ready| R1["TeamLifecycle(reason=ready)"]
    F7 -->|failed| R2["partial_failure → 즉시 cleanup"]
    F7 -->|"timeout/예외"| R3["outcome_unknown<br/>slot 점유 유지, 맹목 재시도 없음"]
    F7 -->|"계약 위반"| R4["invalid_contract → unknown"]
    R1 --> RUN["TeamRunner.ensure_and_bind → 역할 순차 실행 (7장 S4)"]
```

### 5.2 선택 입력의 출처 (bootstrap이 주입, 매 호출 재해석)

| 입력 | 현재 값 | 출처 |
|---|---|---|
| 템플릿 registry | `benchmark-local@1`, `research-local@1` | `fixtures/teams/templates.json`. 각 정의는 template·roles·goal_terms·outputs·minimum_budget을 포함하고 SHA-256 digest가 `APPROVED_PINS`와 일치해야 로드된다 |
| grants | 설치 owner × 모든 DomainId → capability 4종 전체 | `repository.local_principal()` — 사용자 문장의 역할 문자열이 아니라 서버 소유 설치 identity |
| available_capabilities | `evidence_search`, `experiment_run`, `result_analysis`, `evidence_review` | bootstrap 상수 |
| available_runtimes | `{"local"}` (LocalRuntime일 때만), 아니면 빈 집합 | `RuntimeLifecycleSupport` — http/openshell runtime은 팀 lifecycle unsupported |
| budget_ceiling | `max_steps=MAX_GRAPH_STEPS(12)`, `max_tool_calls=MAX_TOOL_CALLS(6)`, `timeout_seconds=TOOL_TIMEOUT_SECONDS(30)` | settings |

템플릿 예산(30 step/10 tool/180 s)보다 ceiling이 작으므로 실제 `execution_budget`은 `min(요청 또는 ceiling, 템플릿)`이고,
그 값이 정의의 `minimum_budget`(benchmark: 6 step/3 tool/30 s) 미만이면 `budget_insufficient`로 거절된다.

### 5.3 패턴별 역할과 capability

| 패턴 | 역할 순서 (supervisor_only) | 역할 → capability | 산출물 outputs |
|---|---|---|---|
| benchmark | supervisor, paper_scout, experiment_runner, result_analyst | paper_scout→evidence_search, experiment_runner→experiment_run, result_analyst→result_analysis, supervisor→없음 | benchmark_report, experiment_summary, evidence_summary |
| research | supervisor, source_scout, evidence_reviewer | source_scout→evidence_search, evidence_reviewer→evidence_review | (registry 정의 참조) |

`TeamSpec` validator가 역할 집합이 패턴과 정확히 같은지, agent_id/memory_namespace가 유일한지, 멤버 domain이 팀 domain과 같은지,
execution_budget이 템플릿 예산을 넘지 않는지 확인한다. `TeamMember.source_ids/tool_names`는 비어 있으며 권한을 부여하지 않는다.
데이터/tool 권한은 실행 시 PolicyPort와 역할 예산이 다시 결정한다.

### 5.4 TeamInstance 상태 전이

```mermaid
stateDiagram-v2
    [*] --> provisioning: reserve_team (reason=reserved)
    provisioning --> ready: prepare 응답 state=ready / 전원 prepared · runtime_ref 있음
    provisioning --> failed: prepare state=failed (partial_failure)
    provisioning --> unknown: timeout · 예외 · invalid_contract
    failed --> cleanup_pending: 자동 cleanup 시작
    ready --> running: TeamRunner 실행
    running --> ready: 실행 종료 (팀 재사용 가능)
    ready --> cleanup_pending: cleanup 요청
    cleanup_pending --> cleaned: cleanup state=cleaned / prepared/unknown 멤버 전원 cleaned
    cleanup_pending --> failed: cleanup_failed
    cleanup_pending --> unknown: timeout · invalid_contract
    unknown --> unknown: 자동 replay 없음 / 운영자 확인 전까지 slot 점유
```

`sandbox_id`는 `mode=REAL`이고 템플릿 `runtime_kind=openshell`일 때만 허용된다(validator). local/mock runtime은 sandbox를 주장할 수 없다.
`TeamLifecycle.trace_collection`은 항상 `uncollected`다. lifecycle은 DB의 durable 증거이지 trace span이 아니다.

### 5.5 PoC 채팅에서의 팀 생성 (P1-008I/J)

채팅에서 행동 동사(검증해/분석해/벤치마크 돌려)가 들어오면 `task_run`으로 판정되고 위 파이프라인이 실행된다.
처리 상태 카드에는 `understanding → routing → team_spawn(패턴·예정 역할, 승인 템플릿 정보) → preparing → team(실제 생성된 구성:
패턴·상태·runtime·역할별 agent_id/capabilities) → team_result → completed`가 순서대로 표시된다.
같은 메시지 재전송은 멱등 재조회이며 두 번째 팀을 만들지 않는다. 이후 관련 질의는 주제어 매칭으로 그 Task 팀에 배정된다.
메모/일반 질의만으로는 Task를 만들지 않는다.

### 5.6 거절·불확실 결과 규칙

| 상황 | 결과 | 사용자에게 보이는 것 |
|---|---|---|
| 선택 실패 (denied/unsupported/unavailable) | `team_selection_denied` — 팀 예약 없음 | 현재 권한·승인·예산으로 팀을 선택할 수 없음 (후보별 사유 코드는 원장에만) |
| 기존 task_id의 goal/status/domain 불일치 | `team_conflict` | 기존 Task 목적/상태 불일치 |
| 저장된 팀 spec ≠ 현재 선택 결과 | `team_conflict` | 승인/권한/실행 조건 재확인 필요 |
| prepare timeout / 예외 | `outcome_unknown` (raw error text 저장 안 함) | 결과 불명, slot 유지 |
| runtime 응답이 계약 위반 | `invalid_contract` → unknown | 동일 |
| 멤버 일부 prepare 실패 | `partial_failure` → cleanup 자동 호출 | 부분 실패 후 정리 |

## 6. 입력 → 라우팅 결정 규칙

### 6.1 비서 intent (INTENT_RULES, first-match)

| 입력 패턴 (예) | intent | 이후 경로 |
|---|---|---|
| "삭제해", "배포해", "결제", "deploy" | unsupported | 즉시 안전 거절 + 대안 안내 |
| "매일", "예약", "리마인드", "cron" | schedule | 예약 문구 파서 → allowlist job |
| "피드백", "앞으로는", "말투" | feedback | 4분류 안내 (선호/정정/공개범위/정책제안) |
| "저장해", "메모해", "기록해" | store_note | private KB 저장 |
| "공개 채널", "외부에", "고객", "공지" | external_draft | 공개 근거 제한 → DRAFT → 수동 승인 |
| "검증해", "분석해", "벤치마크 돌려" (행동 동사만) | task_run | 명시적 팀 실행 (5장) |
| 그 외 | query | 단일 domain 검색/응답 |

### 6.2 Domain 라우팅 (`_route_domain`)

명시 `domain_id`가 최우선. 없으면 키워드: "quant/양자화" → `quantization_research`,
"triv3/벤치마크/근거" → `triv3`. 둘 다 아니면 domain-required 안내(강제 매칭 없음).

### 6.3 채팅 담당 라우팅 (P1-008H, PoC 채팅 전용)

1. 사용자가 domain을 명시하면 해당 domain 담당.
2. 기본(자동): owner의 active/ready Task 목표와 입력의 **구별력 있는 주제어** lexical 매칭
   (샘플/데모 같은 공용 태그는 주제어에서 제외). 매칭되면 기존 Task/Team 재사용.
3. 동점/무관: 비서가 직접 현재 ACL 기준 KB 발췌를 읽어 답한다. 자동 팀 생성 없음.

## 7. 시나리오별 Agent Workflow

### S1. 메모 저장 — "이거 저장해 줘: …"

```mermaid
sequenceDiagram
    participant U as 사용자 (채팅 UI)
    participant C as ChatPort
    participant R as LocalChatRouter
    participant KB as KB 저장 (core API)
    U->>C: POST /chat/stream (store_note)
    C-->>U: stage understanding "입력 이해 중"
    C->>R: 담당 결정 (주제어 매칭)
    C-->>U: stage routing / preparing "자료 저장 준비"
    C->>KB: note 저장 (audience=private, 해당 자료 공간)
    KB-->>C: source_id (idempotent)
    C-->>U: stage completed + 저장 결과
```

모델 호출 없음. 미배정 기본 저장 공간은 TRIV3이며 이는 worker 실행이 아니다.

### S2. 개인 질의 — "오로라 마감 알려줘"

```mermaid
sequenceDiagram
    participant U as 사용자
    participant SVC as AssistantService
    participant SUP as Supervisor Graph
    participant DOM as Domain Graph
    participant POL as PolicyPort (local)
    participant RET as RetrievalPort (local index)
    participant MOD as ModelPort (mock|nvidia)
    U->>SVC: WorkRequest(query, audience=private)
    SVC->>SVC: intent=query, domain 라우팅
    SVC->>SUP: route → delegate
    SUP->>DOM: 단일 domain 실행
    DOM->>POL: authorize (membership 결합, audience 순위만으로 승인 금지)
    DOM->>RET: retrieve (domain namespace, 현재 ACL)
    DOM->>DOM: private marker 사전 검사 + share_egress_filter
    DOM->>MOD: generate (근거 제한 후 호출)
    MOD-->>DOM: 응답 초안
    DOM-->>SUP: DraftBundle (private)
    SUP-->>U: 완료 (private는 수동 승인 불필요)
```

채팅에서 담당 무관 질의(no-match)는 이 경로 대신 비서가 `read_sources`로 KB 발췌만
직접 읽는다(Run 생성 없음).

### S3. 공개 답변 초안 — "외부 공지 초안 만들어 줘" (승인·게시 포함)

```mermaid
sequenceDiagram
    participant U as 사용자
    participant SUP as Supervisor Graph
    participant DOM as Domain Graph
    participant REV as 수동 승인 (PoC 승인함)
    participant RES as ResumePolicy
    participant PUB as ResponsePort (mock|http)
    U->>SUP: external_draft (audience=public)
    SUP->>DOM: delegate — 공개 근거만 먼저 제한
    DOM-->>SUP: 공개 DRAFT (version/hash)
    SUP->>SUP: review → WAITING_APPROVAL
    Note over SUP: LangGraph interrupt — checkpoint에 대기 상태 영속
    U->>REV: 승인/반려 (draft version·hash·target에 결합)
    REV->>RES: resume 요청
    RES->>RES: version/hash/target/source/policy 재검증<br/>내용·대상·ACL·정책 변경 시 재검토
    RES->>PUB: 게시 (durable idempotency)
    PUB-->>U: receipt (현재 mock, 외부 write 금지)
```

timeout은 `outcome_unknown`으로 보존하며 자동 실패/재게시로 바꾸지 않는다.
승희 review 서비스로 교체될 때의 상태 대응은 2.3절을 따른다.

### S4. 명시적 팀 실행 — "TRIV-DEMO 벤치마크 로그 검증하고 비교해 줘"

```mermaid
sequenceDiagram
    participant U as 사용자
    participant SUP as Supervisor Graph
    participant TF as TeamSelector/TeamFactory
    participant TR as TeamRunner
    participant RT as RuntimePort (local|openshell)
    participant PS as paper_scout
    participant ER as experiment_runner
    participant RA as result_analyst
    participant TS as 팀 supervisor
    participant KA as KnowledgeAccumulator
    U->>SUP: task_run intent (+ task_id면 기존 팀 재사용)
    SUP->>TF: delegate_team (5장 파이프라인)
    TF->>TF: 승인 템플릿·grant·capability·runtime·예산으로 선택
    TF->>RT: reserve_team → prepare (Idempotency-Key, timeout, 응답 결합 검증)
    RT-->>TF: TeamInstance(ready | failed→cleanup | unknown)
    TF-->>TR: TeamLifecycle(ready)
    TR->>PS: ① RetrievalPort 검색 → 논문 필터
    TR->>ER: ② benchmark_log_parse (합성 로그, tentative 표기)
    TR->>RA: ③ metric_compare — tool 예산 소진 시 비교 생략 기록
    TR->>TS: ④ 결과 취합 요약 (미검증 건수 명시)
    TS-->>SUP: TeamRunResult → team_draft (owner 검토용 DRAFT)
    SUP->>KA: 파생 지식 제안 (검증 전 가설은 tentative 보존)
    KA-->>U: KB 반영 + 후보(candidates) 갱신
```

research 패턴은 ①source_scout(검색) → ②evidence_reviewer(cited/tentative 분류) →
③supervisor 취합으로 동일 구조다. 역할 간 직접 통신은 없고 모든 입출력이
TeamRunner(Supervisor) 경유이며, step/tool/token/timeout 예산 소진 시 안전 중단과
부분 결과를 반환한다. `TeamRunResult.status=completed`는 모든 역할이 succeeded일 때만 허용되고,
역할별 `RoleOutcome`에는 `simulated` 플래그와 provider가 보고한 token만(모르면 null) 기록된다.
현재 역할 구현은 결정적 local/mock이고 실측 실험이 아니다.

### S5. 예약 — "매일 아침 9시 TRIV3 브리핑해 줘"

```mermaid
flowchart LR
    A["예약 문구"] --> B["결정적 파서<br/>SCHEDULE_JOB_TERMS"]
    B --> C{"allowlist job"}
    C --> D["briefing"]
    C --> E["kb_refresh"]
    C --> F["candidate_scan"]
    D & E & F --> G["5-field cron 변환<br/>내부 UTC, UI Asia/Seoul"]
    G --> H["rfa scheduler 전용 프로세스<br/>APScheduler + SQLAlchemyJobStore"]
    H --> I["실행 시 AssistantService 경유<br/>S2/S4와 동일 권한 경로"]
```

텍스트는 절대 명령/URL/프롬프트가 되지 않는다. 세 job 외에는 존재하지 않으며,
PC 절전 중 실행은 보장하지 않는다.

### S6. 채팅 자동 라우팅 + 단계 스트리밍 (P1-008H/J)

```mermaid
flowchart TB
    IN["채팅 입력 (domain 미지정)"] --> U1["stage: understanding"]
    U1 --> RT{"LocalChatRouter"}
    RT -->|"주제어가 active/ready Task 목표와 매칭"| T1["기존 Task/Team 재사용<br/>stage: routing '담당자 확인' → team"]
    RT -->|"domain 명시"| T2["domain 담당"]
    RT -->|"task_run 행동 동사"| T4["stage: team_spawn → preparing → team (5.5절)"]
    RT -->|"동점/무관"| T3["비서 직접 — 현재 ACL로<br/>KB 발췌 (Run 생성 없음)"]
    T1 & T2 & T4 --> P["stage: preparing → core 실행 (S2/S3/S4)"]
    T3 --> P2["stage: preparing → 발췌 응답"]
    P & P2 --> C["stage: completed + 타임라인/출처 보존"]
```

NDJSON 스트림은 실제 실행 경계의 단계이며 LLM 토큰 스트리밍이 아니다. 처리 상태 카드는 대화 버블 위에 고정되어
단계 detail을 보여주고 새로고침 시 `chat_stages`에서 복원된다. 대화 DB에는 route/stage/source 참조만 저장하고
응답 본문·메모 원문·키는 저장하지 않는다(재표시 시 최신 ACL 재조회).

### S7. 거절·안내 경로

| 입력 | 처리 |
|---|---|
| 삭제/배포/결제 요청 | unsupported — 수행하지 않고 지식 관리 화면/API 안내 |
| 외부/내부 채널의 질의·공개초안 외 요청 | channel-scope 거절 (채널은 query, external_draft만) |
| domain 불명 + 키워드 없음 | domain-required 안내 |
| 피드백 | 4분류 요청. 게시 승인 1회가 공식 정책을 바꾸지 않음 |
| 근거 부족 | "허용된 자료만으로는 근거가 부족합니다" — 추정으로 채우지 않음 |
| 팀 선택 불가 | `team_selection_denied` — 팀 예약 없음 (5.6절) |

### S8. 팀원 서비스 연동 (별도 프로세스)

```mermaid
flowchart LR
    SH["승희 RFA_module<br/>워크플로"] -->|"GET /tasks, POST /tasks/id/ask"| KF["knowledge_facade :8791<br/>정책→audience 제한 검색→모델→canary 치환"]
    KF --> CORE["core application"]
    DY["다영 결재 UI"] -->|"docs/api/inbox.openapi.yaml"| IB["inbox reference :8793<br/>in-memory 표본, 결정 전이·멱등 receipt"]
    DY2["다영 OpenShell"] -.->|"RuntimePort http (prepare/cleanup만, 실행 not_run)"| CORE
```

facade는 근거 없으면 `answer=""`를 반환하고, public 외 audience는 API key 없이 시작을
거절한다. 실제 RFA_module 워크플로 연동·샌드박스 정책 통과는 not_run이다. 모듈 전체 지도는 2장.

## 8. LLMOps — 관측·평가·회귀·보존

LLMOps 축은 네 가지다: (a) 실행 경계마다 남기는 **관측 원장**, (b) 원장을 외부 관측 서버로 내보내는 **trace export**,
(c) 코드 변경 전후를 같은 조건으로 비교하는 **평가 회귀와 release gate**, (d) 로컬·외부 trace의 **보존과 삭제**.
전 축에서 원본은 로컬 SQLite 원장과 JSONL이고, 외부 서버·Judge·NAT는 부가 기능이다. 어느 축도 mock 결과를 실제로 기록하지 않는다.

```mermaid
flowchart LR
    subgraph RUN["실행 (매 요청)"]
        OP["ObservedPort<br/>Model·Retrieval·Policy·Runtime·Response"] --> OBS["Observations<br/>run별 원자적 sequence · alias"]
        OBS --> LED[("SQLite 관측 원장<br/>ObservationLedger 1.1")]
        OBS --> TR["LocalJsonlTrace<br/>TRACE_DIR/rfa-observations-v1<br/>redactor 적용"]
        TR -.->|"opt-in"| LF["LangfuseExportTrace<br/>allowlist span → OTLP/HTTP"]
    end

    subgraph EVAL["평가 (rfa evaluate, 격리 임시 DB)"]
        DS["fixtures/eval<br/>persona-core-v1 · persona-regression-v2 · redteam"] --> EV["deterministic_rule_checks<br/>verify_behavior / verify_regression"]
        EV --> MAN["실행 manifest<br/>digest(dataset·code·evaluator·template·policy)<br/>사례별 규칙·보안 gate·점수·관찰 digest"]
        MAN --> CMP["rfa evaluate-compare<br/>comparable? → 사례 delta → release gate"]
        EV -.->|"ENABLE_JUDGE=nvidia, 합성/공개만"| JUDGE["NvidiaJudge (P1-006A)"]
        SVC2["WorkService 1회 합성 호출"] -.->|"evaluate_synthetic (Python API만)"| NAT["NAT 1.8 eval (spike)"]
    end

    subgraph RET["보존 (일 1회)"]
        R1["TRACE_RETENTION_DAYS (기본 7)<br/>manifest 등록 JSONL만 정리"]
        R2["rfa langfuse-retention<br/>이 앱 trace만 ID 삭제 → 재조회 확인"]
        R3["MinIO mc rm --older-than<br/>원본 OTLP blob"]
    end

    LED --> EV
    TR --> R1
    LF --> R2 --> R3
```

### 8.1 관측 원장 (P1-006D)

| 항목 | 내용 |
|---|---|
| 수집 지점 | `bootstrap.py`가 Model/Retrieval/Policy/Runtime/Response port를 `ObservedPort`로 감싼다. 명시 decorator로만 수집하며 graph 내부 노드는 수집하지 않는다 |
| 경계 종류 | `request, retrieval, policy, model, runtime, approval, tool, publish, internal_nodes, test_sink`. 각 경계는 `collected/uncollected/incomplete`이고 uncollected의 `calls`는 null(0이 아님) |
| 순서·식별 | `observation_id`와 run별 원자적 `sequence`가 실제 수집 순서. `transport=returned/raised`는 port 호출 결과이며 정책 거절·승인 대기와 별개 |
| mode/provider | builtin mock은 `mock`, local runtime은 `local`, reference stand-in은 `provider_kind=reference_http`. 이는 실제 팀원 서비스나 OpenShell 증거가 아니다 |
| started vs 확인 | `started`는 호출 의도이며 call_count를 올리지 않는다. 시작만 남은 crash는 `coverage=incomplete/calls=null` |
| 익명화 | run/session/task 연결의 random alias를 SQLite migration 2에 보존. raw caller ID/hash를 익명화라고 주장하지 않으며 alias↔원본 매핑은 접근 제한 DB |
| 조회 | `container.service.observations.ledger(run_id, principal)` — 소유권 확인 후 `ObservationLedger` 반환. 외부 공개 API 아님 |
| raw tracing 억제 | native LangGraph 실행/재개는 LangSmith `tracing_context(enabled=False, parent=False)`로 환경변수 기반 raw tracing을 끈다 |

### 8.2 로컬 trace와 Langfuse export (P1-006C)

`LocalJsonlTrace`가 원본이다. DB 원장과 대조해 기록한 record만 export 대상이 되고, export 실패는 run을 실패시키지 않는다.

| 항목 | 내용 |
|---|---|
| egress 허가 | `TRACE_BACKEND=langfuse` + `LANGFUSE_EXPORT_ENABLED=true` + loopback `LANGFUSE_BASE_URL` + project key pair가 **모두** 있어야 켜진다. 키만으로는 허가가 아니다. 비loopback은 `endpoint:LANGFUSE_BASE_URL:non_loopback`(not implemented)으로 명시 거절 |
| 전송 | record당 OTLP/HTTP JSON span 1개, `POST /api/public/otel/v1/traces`, Basic Auth, 재시도 없음, timeout ≤5 s. Langfuse v4 `events_only` 모드 때문에 legacy ingestion 대신 OTLP 사용 |
| allowlist | opaque alias, 경계·상태·mode·사유 code, 호출 수, duration, version 참조만. status message·span event·input/output은 비움. 직렬화 body에 설정된 secret이 있으면 `not_attempted: redaction_blocked` |
| token | provider가 보고하지 않은 token은 속성을 보내지 않고 `token_usage=not_reported`. Langfuse 화면의 0은 우리 측정값이 아니다 |
| receipt | `exported/failed/not_attempted` + 고정 사유 code만, 프로세스 메모리 최대 10,000개(비durable). 확인 응답을 받은 2xx만 exported |
| live 검증 | self-host Langfuse 4.46.0(colima)에서 14 관측 export → 14/14 ID 재조회, 저장값에 canary·query·draft·원본 run ID·사용자 ID·키 없음 확인. 닫힌 port는 14/14 `failed: connection_error`에 run은 COMPLETED |

### 8.3 보존과 삭제 (P1-006C/F)

| 대상 | 정책 | 검증 |
|---|---|---|
| 로컬 JSONL | `TRACE_RETENTION_DAYS`(기본 7). `TRACE_DIR/rfa-observations-v1` manifest에 등록된 제품 JSONL만 다음 export 때 정리. SQLite 원장·run metadata·alias는 삭제하지 않음(별도 DB 보존 정책 미구현). 미등록/변조 파일은 안전한 configuration error로 중단 | offline test |
| Langfuse trace | OSS 4.46은 `LANGFUSE_INIT_PROJECT_RETENTION`을 Enterprise entitlement 없이는 무시한다. 대신 `rfa langfuse-retention`이 cutoff 이전 관측을 페이지 조회(40×500, 삭제 500건/회)해 metadata `schema=rfa-langfuse-export-v1`·`service=rfa-mas`인 **이 앱 trace만** `DELETE /api/public/traces/{id}` 후 재조회로 확인한다. cutoff 이후 관측이 하나라도 있는 trace는 보존. exit: completed 0 / partial·failed 1 / not_attempted 2 | live 6 passed(시계 8일 주입 시 2건 삭제·재조회 0건, 다른 앱 trace 보존, dry-run 삭제 0건) + offline 17 |
| MinIO 원본 blob | trace 삭제 후에도 `events/otel/` upload 파일이 남으므로 `mc rm --recursive --force --older-than 7d`로 별도 정리 (stack이 이 앱 전용이라는 전제) | live 확인 |

운영 예시 cron(03:17 KST retention, 03:27 MinIO)은 [evidence/llmops.md](evidence/llmops.md)에 있다.

### 8.4 평가 회귀와 release gate (P1-006B)

| 항목 | 내용 |
|---|---|
| 데이터셋 | `persona-core-v1`(C01~C12), `persona-regression-v2`(4 fixture identity × 6 상황 = 24, seed 29), legacy `evaluation_cases.jsonl`, `redteam_v1.jsonl`(tests helper). 입력 문장의 신원/권한 주장은 권한이 아니며 principal은 항상 고정 fixture identity |
| 실행 | `rfa evaluate --dataset persona-regression-v2 --label <이름> --output <새 파일>`. 격리 임시 SQLite + mock 모델. 충돌·혼입·갱신 사례는 사례마다 격리 컨테이너에 합성 노트를 실제 Knowledge API로 기록 |
| manifest | 입력 digest, dataset digest, policy version, code/evaluator/template digest, 모델 adapter, 런타임 버전, 사례별 규칙 관찰·보안 gate·점수·관찰 digest. 원문 초안·trace·canary는 넣지 않음 |
| 비교 | `rfa evaluate-compare --baseline A --candidate B`. dataset digest·seed·policy·실행 모드·사례 집합이 같을 때만 `comparable=true`; 다르면 이유만 남기고 점수 비교 안 함 |
| release gate | 보안 gate 실패 또는 필수 규칙 실패 1건이라도 있으면 `fail`(exit 1). candidate의 보안 회귀나 상태 하락도 fail. `tests/test_evaluation.py`가 의도적 정책 위반(owner 전용 canary를 모든 요청에 주입)을 candidate로 실행해 `source_scope` 회귀 탐지를 확인 |
| 모드 | `simulated`(기본)와 `actual`은 집계를 합치지 않는다. actual은 `--allow-actual` opt-in이며 provider 없으면 전부 `not_run(actual_provider_unavailable)`. `semantic_quality`, `product_final_gate`는 항상 not_run |
| 측정 이력 | main a0a9347: 19/24 pass(보안 실패 4, private-mixed) → P1-005 screen 통합 후 23/24(보안 0) → P1-001E 후 24/24. 서로 다른 code digest이므로 `evaluate-compare`로 직접 비교하지 않고 관측치만 기록 |

### 8.5 Judge (P1-006A)

`ENABLE_JUDGE` + `JUDGE_PROVIDER=nvidia` + 키는 선택이지 허가가 아니다. 매 호출 `SyntheticPublicJudgeGate`가 합성/공개 초안만 통과시키고
private marker가 있으면 거절한다. mock fallback은 없다. Judge 점수는 권한 gate를 상쇄하지 못한다.
실제 NVIDIA Ultra Judge 실행은 공개 합성 사례 n=1(score 0.6, 2.24 s)이며 Persona 전체의 Judge 평가나 사용자 만족도 측정이 아니다.

### 8.6 NVIDIA Agent Toolkit (NAT) 평가 (P0-027 spike / P0-028)

`adapters/nat_eval.py`의 `evaluate_synthetic(ready_container, request, principal)`이 신뢰된 합성 WorkService 호출 1회를
NAT 1.8(`nvidia-nat-core/langchain/eval`) `rfa_contract` evaluator로 평가한다(`configs/nat/rfa_eval.yml`, `rfa_eval_cases.json`,
case `representative-v1`). Python API만 있고 CLI/`ENABLE_NAT` 설정·lifecycle 교체·외부 exporter·실제 provider 주장은 없다.
요청과 identity는 task-local ContextVar에 두며 NAT 메시지/config에 넣지 않는다. 설치된 v1.8 API는 experimental이며 알 수 없는 버전은 fail-closed다.
경계·한계(메시지 입출력, checkpoint/HITL 미지원, usage null, 예외 텍스트 정규화)는 [NAT_COMPATIBILITY.md](NAT_COMPATIBILITY.md)에 있다.

### 8.7 운영 명령 요약

| 명령 | 용도 | 출력 원칙 |
|---|---|---|
| `rfa doctor` | 변수 이름의 configured/missing만 표시 | 값·키 출력 없음 |
| `rfa openapi --app core\|knowledge-facade` | 생성 OpenAPI 계약 export | `docs/api/*.json` |
| `scripts/contract_baseline.py check\|check-extended` | DTO/계약 baseline 회귀 | `docs/contracts/baseline.json`, `extended.json` |
| `rfa evaluate` / `rfa evaluate-compare` | 8.4 | manifest 새 파일만, 덮어쓰기 없음 |
| `rfa langfuse-retention [--dry-run]` | 8.3 | 개수·cutoff·고정 사유 code만 |
| `rfa scheduler` | 예약 job 전용 프로세스 | API worker에서 시작 금지 |
| `RFA_LANGFUSE_LIVE=1 pytest tests/integration/test_langfuse_live.py` | opt-in live export/삭제 검증 | skip은 증거가 아님 |

## 9. 실제/mock 경계 요약

- **실제 local 코드**: 저장·상태 전이·권한(authorize)·팀 선택/예약/중복 방지·예산 강제·예약·승인
  version/hash 검증·idempotency·chat 라우팅·관측 원장·로컬 보존. 이 부분은 mock이 아니다.
- **결정적 mock/합성**: 팀 역할 산출물(논문/실험 로그/비교), 기본 모델 응답, 게시 receipt,
  Tool 결과, 평가 회귀의 모델(mock-model). 화면·로그·manifest에 simulated/mock 표기를 보존한다.
- **실검증된 실제 경로**: NVIDIA 모델 실호출(P1-002), NVIDIA Judge n=1(P1-006A), self-host Langfuse export·ID 삭제·retention sweep(P1-006C/F).
  팀 role → 실제 모델 연결, OpenShell runtime, 승희 review 실 어댑터, 실게시, NAT 제품 경로는 각각 별도 gate로 남아 있으며
  mock 성공을 실제 성공으로 기록하지 않는다.
- **stand-in**: `reference/local_response`, `reference/local_runtime`, `inbox reference`, `ui/`는 팀원 모듈 자리를 채우는
  로컬 구현이며 팀원 서비스 호환의 증거가 아니다(2장).
- **외부 write**: `ALLOW_EXTERNAL_WRITES=true`여도 P0에서는 금지. 비loopback Langfuse도 거절.
