# 통합 기준

이 문서는 현재 구현된 기반의 port/DTO 경계와 교체 가능한 서비스의 임시 HTTP 계약을 설명한다. `src/rfa_mas/reference/app.py`는 **우리 reference contract를 검증하기 위한 로컬 fixture**이며 어느 production 구현의 호환성 증거도 아니다. 실제 계약이 확인되면 adapter와 DTO mapper를 교체한다. 승인 원본은 승희 서비스, runtime identity와 sandbox 운영 원본은 다영 서비스가 소유하며 이 저장소의 mock/mirror는 별도 원본 권한을 갖지 않는다.

## 확정 아키텍처와 구현 상태의 구분

2026-09-26에 확장한 P0 계획과 상태의 원본은 `tasks/<ID>/task.yaml`이며 [TASKS.md](../TASKS.md)는 생성된 조회 view다. 아래 기존 endpoint/DTO 표는 현재 schema 1.0 구현을 설명한다. 새 Session/Task/TeamSpec/Scheduler/callback/receipt API가 이미 제공된다는 뜻이 아니다. 최소 계약 변경은 P0-014, reference 확장은 P1-008에서 구현·검증 후 이 문서에 반영한다.

기존 Pydantic/OpenAPI의 repository-local provisional baseline은 [contracts/baseline.json](contracts/baseline.json), 합성 정상·거절·근거 부족·부분 실패·timeout fixture는 `fixtures/contracts/reference_cases.json`이다. `.venv/bin/python scripts/contract_baseline.py check`는 export·schema·fixture 형식을 검사하고, 실제 local/reference 응답은 `.venv/bin/python -m pytest -q tests/test_contract_baseline.py`로 대조한다. 생성 방향·version/digest 및 후속 변경은 [CONTRACT_CHANGELOG.md](CONTRACT_CHANGELOG.md)를 따른다. 이 baseline의 성공은 실제 팀원 API 지원이나 새 확장 schema 완료가 아니다.

- 자체 UI와 FastAPI/LangGraph 코어를 유지한다. UI 구현은 다영 모듈을 연결하며 코어는 안전한 polling 상태·이벤트를 제공할 계획이다.
- 세션은 서비스 메타데이터와 LangGraph 영속 SQLite checkpointer, 장기 지식은 별도 KB 모델을 사용한다. 현재 `graph_checkpoints`는 최종 metadata일 뿐 재개 가능한 checkpointer가 아니다. 현재 run GET에는 사용자별 owner 조회 제한도 없어 P0-015/016이 선행한다.
- 예약은 SchedulerPort 아래 APScheduler 3.x의 영속 job store를 단일 프로세스가 소유하도록 구현할 계획이다. FastAPI worker마다 시작하거나 직접 cron 엔진을 만들지 않는다.
- OpenClaw Gateway/Deep Agents는 필수 dependency가 아니다. 내부 소스 복사·비공개 import는 하지 않으며 후속 도입은 worker/ChannelAdapter 교체로 제한한다.
- 현재 `TaskRequest`는 일회 위임 DTO다. 지속 Task·Task당 활성 팀 하나·여러 Run·session↔Task N:M 관계는 P0-014/019에서 추가하며 기존 port 이름을 유지한다.
- 기밀 검수 소유권은 미확정이다. 결정적 PolicyPort와 선택적 LLM 보조 검수를 분리하며 읽기·공유·cloud 전송 권한을 각각 검사하도록 P1-005에서 보강한다.

새 API·설정·명령을 현재 사용 가능한 목록에 올리는 시점은 해당 task의 구현과 검증이 끝난 뒤다. 이 문서에서 과거 P0의 완료 범위는 기반 local/mock 계약에 한정된다.

### 후속 추적·평가·context 계약(아직 planned)

P0-014의 RFA-EXTENDED 1.1에 [Execution/Evidence/Policy/Draft/Eval 변경안](EVALUATION_CONTEXT.md#a--repository-local-계약-변경안)을 함께 반영한다. 기존 Pydantic DTO를 확장하고 typed trace allowlist·선택적 context 읽기·7종 정상/오류 fixture를 검증한 뒤에만 새 digest를 발행한다. 아직 없는 session/team/sandbox/approval/receipt ID를 채우지 않으며 trace ID는 인증이 아니다. 정책 결정은 trusted principal·자료 라벨에서만 도출한다.

공통 trace 구현은 P1-006D, NAT wrapper는 P0-027/028, 평가와 레드팀은 P1-006/006E, KB 선택 loader는 P1-001B가 소비한다. NAT는 FastAPI/checkpointer/Task 상태 원본을 대체하지 않는다. 서비스 간 trace 전파·proof 검증·정책/ACL/본문/대상 변경 뒤 재검토·unknown 조회는 P1-008의 adapter 책임이다. 상대 승인/게시/runtime 원본과 OpenShell middleware/gateway는 우리 reference의 소유 범위가 아니다. 실제 팀원 gate P1-008A/B 및 OpenShell P1-007B는 별도로 남긴다.

## 의존성 방향과 composition

```text
FastAPI / CLI
      |
      v
WorkService -> Supervisor Graph -> RuntimePort -> Domain TaskGraph
                                      |               |
                                      |               +-> PolicyPort
                                      |               +-> RetrievalPort
                                      |               +-> ModelPort
                                      +-> ResponsePort

bootstrap.py: settings에 따라 port 구현을 조립
```

규칙은 다음과 같다.

- 호출 방향은 `API -> application/graph -> port`다.
- `src/rfa_mas/bootstrap.py`만 backend를 선택하고 dependency injection을 수행한다.
- graph node는 HTTP 주소, 인증 header, MCP SDK, OpenShell CLI를 import하거나 mock/real 분기하지 않는다.
- Pydantic 모델과 FastAPI가 생성하는 OpenAPI가 이 서비스 공개 계약의 기준이다.
- 도메인 오류를 HTTP 상태로 바꾸는 책임은 `src/rfa_mas/api/app.py`와 outbound HTTP adapter 경계에만 둔다.
- 선택한 real/reserved backend가 준비되지 않았으면 `configuration_error` 또는 `not_implemented`로 실패하며 mock으로 자동 fallback하지 않는다.

## 로컬 서비스 topology

서비스 구현 주체는 port 계약과 분리한다. 로컬 기본 endpoint는 다음과 같이 할당하고, 통합 계약 fixture는 계속 `127.0.0.1:8001`에서 별도로 실행한다.

| Port | 서비스 | 인증 방향 |
| ---: | --- | --- |
| 8000 | RFA Supervisor API | client → RFA: `APP_API_KEY` |
| 8001 | 통합 reference fixture | 테스트 전용; 현재 bearer 검증 없음 |
| 8011 | Response/Review | RFA → Review: `RESPONSE_API_TOKEN` |
| 8012 | MCP/Tool gateway | RFA/Runtime → Tool: `TOOL_API_TOKEN` |
| 8013 | Agent Runtime | RFA → Runtime: `RUNTIME_API_TOKEN` |
| 8014 | Policy decision | Domain Graph → Policy: `POLICY_API_TOKEN` |
| 7670 | NeMo Retriever service boundary | RFA/Runtime → Retriever: `NEMO_RETRIEVER_API_TOKEN` |
| 3000 | Langfuse | trace producer → Langfuse project key pair |

`uv run rfa init-env --output .env.dev`는 각 내부 통신 구간에 서로 다른 credential을 생성하고 profile을 `0600`으로 저장한다. `uv run rfa --env-file .env.dev <command>`로 명시적으로 로드한다. 실제 수신 서비스는 대응 값을 별도 secret store에서 받아 constant-time 비교 등으로 검증해야 한다. 현재 reference fixture는 authorization header를 검증하지 않으므로 token 생성만으로 fixture 보안이 강화됐다고 주장하지 않는다. Langfuse key pair는 self-host 초기화에도 같은 pair를 주입해야 하고, `NVIDIA_API_KEY`는 외부 발급값이라 자동 생성하지 않는다.

현재 main Work 경로는 `RuntimePort`와 `ResponsePort`를 직접 사용한다. `ToolPort`는 아직 실행 경로에 연결되지 않았고, `RUNTIME_BACKEND=http`이면 8013 Runtime이 Domain TaskGraph를 호스팅하면서 Policy/Retriever/Model 경계를 호출하도록 구현해야 전체 topology가 실제로 사용된다.

## Port와 P0 구현

| Port | 책임 | P0 기본 구현 | 교체 구현 | 현재 주장 범위 |
| --- | --- | --- | --- | --- |
| `ModelPort` | 근거 제한 생성·구조화 응답 | `MockModel` | NVIDIA adapter는 P1 | mock 생성만 검증 대상 |
| `RetrievalPort` | domain/권한 범위의 근거 검색 | `MockRetrieval` + SQLite fixture | NeMo Retriever CLI/service는 P1 | 실제 RAG/skill 연결 아님 |
| `ResponsePort` | 검수 DRAFT 제출, 결정 조회 | `MockResponse` | `ResponseHttpAdapter` | 승희 서비스 호환성 미검증 |
| `ToolPort` | 구조화 tool 요청/결과 | `MockTool` | `ToolHttpAdapter` | MCP나 외부 채널 실행 아님 |
| `RuntimePort` | `AgentSpec` task 실행·상태·취소 | `LocalRuntime` | `RuntimeHttpAdapter` | process-local이며 OpenShell sandbox 아님 |
| `PolicyPort` | 자료 접근·공유·tool 정책 | `LocalPolicy` | `PolicyHttpAdapter` | application policy이며 OS 강제 아님 |
| `JudgePort` | 근거 충실도·질문 해결도·작업 후보 유용성의 비권위적 보조 평가 | `MockJudge` | 실제 Judge는 P1 opt-in | privacy/access 규칙은 application의 결정적 evaluator가 별도 판정 |
| `WorkRepositoryPort` | run, DRAFT, checkpoint, KB | `SqliteWorkRepository` | 미정 | 다른 서비스 DB를 공유하지 않음 |
| `TracePort` | metadata trace | `LocalJsonlTrace` | Langfuse는 reserved | 본문 관측이나 live LLMOps 아님 |

## 공통 DTO

계약 버전은 `schema_version="1.0"`이다. DTO 정의는 `src/rfa_mas/contracts/`에 있다.

| DTO | 용도와 중요한 불변조건 |
| --- | --- |
| `WorkRequest` / `RunResult` | 최상위 작업 요청/결과. `request_id`, `trace_id`, `run_id`, `agent_id`, 선택적 `domain_id`, `idempotency_key`를 전달한다 |
| `AgentSpec` | domain, memory namespace, capability, instruction ref, step/tool budget. credential을 포함하지 않는다 |
| `TaskRequest` / `TaskResult` | Supervisor가 runtime을 통해 domain task를 위임하고 회수하는 계약. 회수 시 request/trace/run/agent/domain과 output scope를 원 요청에 다시 결합해 검사한다 |
| `EvidenceBundle` | 허용된 `EvidenceItem`만 포함하며 source revision, 위치, audience, content hash, policy version을 보존한다 |
| `DraftBundle` | `draft_id`, `version`, 정확한 `target`, 허용 근거 reference와 본문을 포함한다. `content_hash=sha256(content)`와 `audience=target.audience`를 강제하며 private 원문과 내부 graph state는 포함하지 않는다 |
| `ReviewDecision` | draft ID/version/hash/target에 결합한다. 본문이나 target 변경 후 이전 승인을 재사용할 수 없다 |
| `FeedbackEvent` | style, factual correction, personal disclosure preference, official-policy-change proposal를 구분한다 |
| `ToolRequest` / `ToolResult` | effect를 `read`/`write`로 구분하고 idempotency key 및 outcome을 전달한다 |
| `PolicyDecision` | deterministic 접근 판단, 안전한 이유, policy version, 허용 audience를 전달한다 |
| `StructuredError` | `code`, `retryable`, 안전한 `message`와 추적 ID를 제공한다. 비밀이나 내부 stack을 전달하지 않는다 |
| `EvaluationCase` / `EvalResult` | persona, 자료 scope, 기대/금지 정보와 결정적 규칙 결과를 보존하고 actual/mock/미실행 Judge 보조 평가를 분리한다 |

서버는 request body의 `user_id`, membership, audience 권한 주장을 신뢰하지 않는다. `TrustedPrincipal`은 인증 경계에서 생성해 application에 주입한다. P0의 loopback 기본 principal은 fixture용 개발 identity일 뿐 production 인증이 아니다.

## 상태 소유권

아래 표는 목표 production 원본 소유권이다. 동일 계약의 로컬 구현을 우리가 먼저 만들 수 있지만, 교체 시 차이는 성능뿐 아니라 검색 filter/ranking, 승인자 identity, idempotency, publication receipt와 상태 원본 의미까지 포함할 수 있으므로 contract test로 확인한다.

| 소유 서비스 | 원본으로 소유하는 상태 | 이 서비스와의 교환 |
| --- | --- | --- |
| 이 저장소/서비스 | KB, run 상태, domain task memory namespace, DRAFT version/hash | Pydantic DTO와 API |
| 승희 서비스 | 사용자 승인 결정 원본, 게시 실행/결과/영수증 | `ResponsePort`, `ToolPort` |
| 다영 서비스 | sandbox lifecycle, 실제 권한 강제, runtime identity | `RuntimePort` |
| 기밀 검수 Agent | 최종 소유권 미확정 | P0는 local policy와 교체 지점만 제공 |

외부 승인 상태를 `RunResult`에 반영할 수 있지만 이 서비스가 별도의 승인 권한을 만들지는 않는다. 다른 서비스의 DB 또는 LangGraph checkpoint를 직접 읽거나 공유하지 않는다.

## 실행 상태

작업 상태와 검수/게시 상태는 별도 enum이다.

```text
created -> running -> waiting_approval -> completed
                   \-> failed
                   \-> cancelled
                   \-> outcome_unknown
```

`created -> cancelled`, `running -> completed/failed/cancelled/outcome_unknown`도 허용된다. `outcome_unknown`은 runtime task 생성처럼 부작용 가능 호출의 timeout을 실패로 단정하지 않기 위한 terminal 상태다. `completed`, `failed`, `cancelled`, `outcome_unknown`에서 추가 전이는 거절한다.

이는 현재 state machine이다. durable interrupt/resume와 effect ledger는 아직 없다. P0-021은 대기/중단 재개, failed/cancelled의 명시적 새 run 재시도, outcome_unknown의 별도 대사 기록을 추가한다. 현재 terminal run을 재실행해서 부작용 결과를 추정하거나 이미 대사 기능이 있다고 설명하지 않는다.

- `ReviewStatus`: `pending`, `approved`, `revision_requested`, `rejected`
- `PublicationStatus`: `not_requested`, `pending`, `succeeded`, `failed`, `outcome_unknown`
- `ResultStatus`: `succeeded`, `failed`, `denied`, `timed_out`, `outcome_unknown`

P0 success의 `completed`는 mock 검토 흐름이 끝났다는 뜻이다. 외부 게시가 완료되었다는 뜻이 아니며 기본 `publication_status`는 `not_requested`다.

## Provisional HTTP reference contract

기준 구현은 `src/rfa_mas/reference/app.py`, client는 `src/rfa_mas/adapters/http.py`다. JSON body는 각 Pydantic 모델의 `model_dump(mode="json")` 형태다.

| Method / path | 요청 | 응답 | idempotency / timeout |
| --- | --- | --- | --- |
| `POST /v1/reviews` | `{draft: DraftBundle, simulation_scenario}` | `ReviewDecision` | `Idempotency-Key` 필수. 자동 재시도 없음. timeout은 review `pending`, publication `not_requested` |
| `GET /v1/reviews/{draft_id}` | 없음 | `ReviewDecision` 또는 404 | read retry 허용 |
| `POST /v1/tools/execute` | `ToolRequest` | `ToolResult` | 요청 idempotency key 전달. read effect만 제한 재시도; P0 write는 network 전에 거절 |
| `POST /v1/policy/decisions` | `PolicyRequest` | `PolicyDecision` | side effect 없는 판단으로 제한 read retry 허용 |
| `POST /v1/runtime/tasks` | `{spec: AgentSpec, request: TaskRequest}` | `TaskResult` | 요청 idempotency key 전달. 자동 재시도 없음; timeout은 outcome unknown 오류 |
| `GET /v1/runtime/tasks/{run_id}` | 없음 | `TaskResult` 또는 404 | read retry 허용 |
| `POST /v1/runtime/tasks/{run_id}/cancel` | 없음 | `TaskResult` | 자동 재시도 없음; timeout은 outcome unknown 오류 |

Outbound 인증은 configured token을 `Authorization: Bearer ...`로 보낸다. token/header는 로그·trace·오류에 넣지 않는다. `HTTP_TIMEOUT_SECONDS`는 요청 timeout, `MAX_READ_RETRIES`는 최초 시도 이후 허용하는 read 재시도 수다.

P0의 네 HTTP adapter는 loopback URL만 허용한다. non-loopback URL은 `not_implemented`로 실패하고, `ToolEffect.WRITE`는 loopback 여부와 관계없이 transport 호출 전에 거절한다. 따라서 아래 fixture 검증은 실제 팀원 서비스나 외부 write 검증이 아니다.

Reference fixture를 수동으로 띄우려면 별도 loopback port를 사용한다.

```bash
uv run uvicorn rfa_mas.reference.app:create_reference_contract_app \
  --factory --host 127.0.0.1 --port 8001
```

이는 local contract fixture일 뿐 팀원 서비스가 아니다. fixture는 process memory에 review/runtime 상태를 보관하므로 재시작하면 사라진다.
기본 `LocalRuntime`은 실행과 terminal status 조회만 제공하며 P0 cancel은 `not_implemented`로 명시 실패한다. reference HTTP fixture의 cancel 응답은 DTO 계약 시험용이며 실제 sandbox task 취소 증거가 아니다.

## Adapter 교체 절차

### 실제 계약이 reference contract와 같을 때 — P1

1. 상대 OpenAPI와 인증·권한·idempotency·timeout 의미를 합의한다.
2. P0의 loopback-only gate를 실제 runtime credential/egress 정책 경계로 교체한다.
3. 우리 또는 팀원 구현의 base URL과 인증 token을 `.env`에 설정하고 필요한 backend를 `http`로 선택한다.
4. `uv run rfa doctor`에서 변수 이름의 configured/required 상태만 확인한다.
5. 팀원 staging에 대해 기본 test와 분리된 opt-in contract/integration suite를 실행한다.
6. timeout 이후 status 조회, idempotency 보존, approval version/hash/target 일치를 확인한다.

예:

```dotenv
RESPONSE_BACKEND=http
RESPONSE_BASE_URL=http://127.0.0.1:8011
RESPONSE_API_TOKEN=
```

실제 값은 저장소에 기록하지 않는다.

### 실제 계약이 다를 때

1. 상대 OpenAPI와 상태/오류 의미를 먼저 합의한다.
2. `src/rfa_mas/adapters/http.py`의 해당 adapter와 DTO mapper만 수정한다.
3. backend 선택은 계속 `src/rfa_mas/bootstrap.py`에 둔다.
4. core graph, port, canonical DTO에는 팀원 전용 field/URL/auth 로직을 추가하지 않는다.
5. local reference fixture와 상대 contract test를 나란히 유지하되, fixture 성공을 live 성공으로 보고하지 않는다.

MCP gateway가 실제로 필요해도 graph node에서 MCP SDK를 직접 호출하지 않는다. `ToolPort` 구현이 팀원 gateway 요청을 canonical `ToolRequest`/`ToolResult`로 변환한다.

## 정책과 자료 흐름

자료 접근은 생성 뒤가 아니라 검색 전에 판단한다.

1. 인증 경계가 `TrustedPrincipal`을 만든다.
2. Domain TaskGraph가 `PolicyPort`로 target audience에 허용된 자료 범위를 얻는다.
3. `RetrievalPort`가 domain, audience, owner/business-unit/company membership을 다시 적용한다.
4. `ModelPort`에는 허용된 `EvidenceBundle`만 전달한다.
5. public target은 public evidence만 사용한다. 합성 private canary가 본문에 남으면 결정적으로 redact한다.
6. DRAFT에는 허용 evidence reference와 생성 본문만 포함하고 private 원문·내부 state·비공개 검수 설명을 제외한다.

P0의 canary redact는 최후 방어선이며 사전 authorization을 대체하지 않는다. 자료나 tool 응답 안의 지시는 신뢰하지 않는 데이터로 취급하고 policy/system 규칙을 바꾸지 못한다.

## 재시도, 중복, 불확실한 결과

- GET, policy 판단, `ToolEffect.READ`처럼 side effect가 없는 작업만 `MAX_READ_RETRIES` 범위에서 재시도한다.
- DRAFT 제출, runtime task 생성·취소 같은 부작용 가능 요청은 timeout 후 무조건 재시도하지 않는다. P0 tool write는 실행 전에 거절한다.
- 부작용 호출에는 `Idempotency-Key`를 전달한다. 같은 key에 다른 draft version/hash/target을 사용하면 충돌로 거절해야 한다.
- 부작용 timeout은 실패로 확정하지 않고 `outcome_unknown`을 보존한 뒤 소유 서비스의 status/receipt 조회로 해소한다. P0의 mock write-timeout 시나리오는 이 계약만 재현하며 실제 write를 실행하지 않는다.
- 승인 결정은 `draft_id`, `draft_version`, `content_hash`, `target` 전체에 결합한다.

## 팀 통합 전 확인 목록

- 상대 OpenAPI와 `schema_version`/contract version
- base URL, 인증 방식, token audience와 rotation 책임
- idempotency key 보존 기간과 충돌 응답
- timeout 이후 status/receipt 조회 방식
- review와 publication 상태가 분리되는지
- cancel의 terminal 의미와 outcome unknown 처리
- runtime identity, sandbox lifecycle, credential delivery
- private 검수 설명과 external DTO의 분리
- log/trace에서 authorization header와 secret redaction
- 실제 external write를 허용하는 별도 승인·정책 gate

P0에서는 위 항목이 합의되지 않아도 mock/local 흐름을 실행할 수 있다. 합의 전에는 `registry/service.json`과 이 계약을 `provisional`로 유지한다.
