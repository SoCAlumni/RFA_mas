# 교육 자료와 구현 매핑

이 문서는 NVIDIA DLI 과정의 개념을 이 저장소의 P0 경계에 연결한다. 교육 자료의 예제와 이 프로젝트의 구현은 같은 것이 아니다. 아래 상태는 구현 성격을 나타내며, 실행 성공 여부는 별도의 검증 결과로만 확정한다.

- `실제(local)`: 외부 서비스 없이 이 프로세스에서 실행하도록 만든 실제 제어 흐름 또는 결정적 정책. OpenShell 보안 경계를 뜻하지 않는다.
- `모의(P0)`: 계약과 실패 처리를 검증하기 위한 결정적 mock. NVIDIA API, NeMo Retriever, MCP 또는 팀원 서비스 호출 성공을 뜻하지 않는다.
- `계획(P1)`: 후속 단계에서 실제 외부 구성요소에 연결할 작업.
- `미검증`: 공식 지원 범위, 상대 서비스 계약 또는 실제 배포에서 아직 확인하지 않은 가정. 지원된다고 주장하지 않는다.

P0 local/mock 증거는 2026-09-25 KST에 offline suite와 demo로 검증했다. 아래 `검증 완료(P0)`는 해당 로컬 경계만 뜻하며 NVIDIA API, NeMo Retriever, MCP, NemoClaw 또는 OpenShell live 검증으로 확대 해석하지 않는다.

2026-09-26 계획 보강: [TASKS.md](../TASKS.md)의 P0는 세션·Task 팀·예약·복구까지 확장되었으며 아직 완료되지 않았다. 기존 기반 suite는 이번에도 170개 통과했지만 새 기능이나 실제 NVIDIA 기술 검증 증거는 아니다. 기밀 검수 최종 소유권은 계속 미확정이다.

| 교육/기술 경계 | 구현 task | 파일/예정 위치 | 실행 증거 상태 |
| --- | --- | --- | --- |
| 상태·세션·장기 지식 분리 | P0-015/016/021, P1-001/001A | 기존 `application/service.py`, `adapters/local.py`; 신규 예정 `adapters/checkpoints.py`(모두 `src/rfa_mas/` 아래) | 기존 metadata 저장만 verified; durable resume not_run |
| 역할 위임·도구·종료 예산 | P0-018/019/020 | 기존 `src/rfa_mas/application/graphs/`; 신규 예정 `application/teams.py`, `application/workers.py` | 단일 domain graph만 verified; 팀 실행 not_run |
| NVIDIA 추론 API | P1-002/002A | `tests/integration/test_nvidia_live.py`(direct hosted smoke), `docs/evidence/nvidia-model.md`; 제품 adapter는 신규 예정 `src/rfa_mas/adapters/nvidia.py` | hosted Nemotron 3.5 Lightning 합성 호출 live verified(2026-09-26, P1-002A); 제품 ModelPort 경로 not_run |
| 공식 NeMo Retriever Skill | P1-003/003A | `tests/integration/test_retriever_live.py`(공식 Skill CLI direct smoke), `docs/evidence/nvidia-skill.md`; 제품 adapter는 신규 예정 `src/rfa_mas/adapters/nemo_retriever.py` | 26.8.1 CLI ingest/query·hosted embedding·근거 기반 답변 live verified(2026-09-26, P1-003A); 제품 Research worker not_run |
| 지원 runtime의 NemoClaw 운영 | P1-007/007A | [NemoClaw 증거](evidence/nemoclaw.md)(공식 지원 범위·최소 API·identity·egress·배치 경계·합성 입력 계획), `docs/INTEGRATION.md` | 설계 문서화(P1-007, 2026-09-27). 공식 지원 agent는 OpenClaw/Hermes/Deep Agents Code뿐이고 목록 외 harness는 Unsupported. 이 서비스의 NemoClaw 실행 real not_run(quickstart 미실행: inference credential·자원 blocker) |
| OpenShell 권한 강제 | P1-007C, P1-008B, P1-007B | [OpenShell 증거](evidence/openshell.md)(E2E-05, `scripts/openshell_e2e05.py`, `deploy/openshell/`, `tests/integration/test_openshell_live.py`); [NemoClaw 증거 §8](evidence/nemoclaw.md)(OpenShell 단독 첫 관측); 기존 `src/rfa_mas/adapters/http.py` | real(OpenShell local standalone): 이 Mac의 공식 OpenShell v0.1.1 gateway(VM compute driver)에서 stand-in 조사/실행 역할 정책의 파일·네트워크·실행 허용/차단 matrix를 합성 자료로 재현(run 0926181417, 2026-09-27 KST, opt-in live 5 passed). RFA 제품 RuntimePort가 OpenShell에서 역할을 실행하는 경로·팀원 identity(P1-008B)·NemoClaw(P1-007A)는 not_run; local runtime은 sandbox 아님 |

예정 파일/증거 경로는 아직 산출물이 아니다. 2026-09-27 재조회에서 learn.nvidia.com 과정 페이지(`course-v1:DLI+S-FX-43+V1`)는 HTTP 200이었지만 본문이 JavaScript로 채워져 정적 조회로 읽지 못했다. 아래 학습 목표는 같은 course ID를 공식 과정 페이지로 링크하는 NVIDIA DLI 저장소의 canonical 문구만 사용한다. 미확인 제출 미션, 필수 기술 수, 수료·평가 조건은 추가하지 않는다.

## 과정 학습 목표 → task → 파일 → 실행 증거 (P1-007)

학습 목표는 [COURSE_CANON.md](https://github.com/NVDLI/NemoClawDLI/blob/main/web/nemoclaw/COURSE_CANON.md)의 원문이다(2026-09-27 확인). 상태 표기에서 real(local)은 외부 서비스 없는 실제 로컬 제어 흐름, mock은 결정적 모의 공급자, real(hosted/OpenShell)은 해당 외부 구성요소를 실제로 실행한 결과, not_run은 미실행, unverified는 공식 지원이나 동작을 확인하지 않은 것이다. 각 task의 최신 검증·통합 상태는 `tasks/<ID>/task.yaml`이 원본이다.

| 학습 목표 | task | 파일 | 실행 증거 | 상태 |
| --- | --- | --- | --- | --- |
| Build a basic agent loop and identify its core components. | P0 기반, P0-018/019/020 | `application/graphs/supervisor.py`, `graphs/domain.py`, `application/workers.py` | `tests/test_graph.py`, `tests/test_supervisor_boundaries.py`, `tests/test_team_execution.py` offline suite | real(local) 제어 흐름 + mock 모델 |
| Implement reliable tool use and function calling within an agent system. | P1-002A, P1-002 | `tests/integration/test_nvidia_live.py`, [모델 증거](evidence/nvidia-model.md); 제품 `ToolPort`는 `adapters/mock.py` | hosted Nemotron의 named tool_choice 제안 live 관측(도구 실행 0회) | real(hosted, 제안만); 제품 tool 실행 경로 mock; 제품 ModelPort NVIDIA 경로 not_run |
| Design and coordinate multi-agent systems using structured routing patterns. | P0-018/019/020 | `application/team_selector.py`, `teams.py`, `workers.py`, `graphs/supervisor.py` | `tests/test_team_selector.py`, `tests/test_teams.py`, `tests/test_team_execution.py` offline suite | real(local) + mock 공급자; sandbox 위 다중 agent 실행 not_run |
| Utilize OpenShell to configure agent identities and ensure safe, sandboxed operations. | P1-007, P1-007C, P1-007B, P1-008B | [NemoClaw 증거](evidence/nemoclaw.md) §4~§8, [OpenShell 증거](evidence/openshell.md) | OpenShell v0.1.1 단독 gateway에서 stand-in 역할별 정책(조사/실행)의 허용·차단을 OCSF 기록과 policy differential로 판정(P1-007C, E2E-05) | real(OpenShell local standalone, stand-in 역할 정책, 합성 자료); RFA identity 매핑·제품 역할 실행 경로 not_run; RFA backend는 sandbox 밖 |
| Deploy and manage autonomous agents while building persistent skill libraries. | P1-007A, P1-003A, P1-003 | [NemoClaw 증거](evidence/nemoclaw.md), [Skill 증거](evidence/nvidia-skill.md), `tests/integration/test_retriever_live.py` | 공식 NeMo Retriever Skill CLI direct live 실행(P1-003A). NemoClaw 배포·skill 설치 미실행 | Skill CLI real(direct); NemoClaw 운영 not_run; 이 서비스의 NemoClaw 지원 unverified |

## 계층 구분

| 계층 | 이 프로젝트에서의 역할 | P0 경계 | 상태 |
| --- | --- | --- | --- |
| 모델 추론 API | 텍스트 또는 구조화 응답 생성 | `ModelPort` 뒤의 결정적 mock | 제품 경로는 모의(P0); hosted NVIDIA 직접 호출은 합성 요청으로 live 확인(P1-002A), 제품 연결은 계획(P1-002) |
| Agent Skill | worker가 특정 기능을 사용하는 절차와 지침 | skill을 설치하거나 실행하지 않음 | NeMo Retriever Skill 절차는 저장소 밖 pinned CLI로 실제 실행(P1-003A); 제품 worker 연결은 계획(P1-003) |
| LangGraph | 상태, node, edge, routing, 종료 조건을 구성하는 orchestration | Supervisor와 공통 Domain TaskGraph | 실제(local), 검증 완료(P0) |
| NemoClaw | 지원 agent runtime의 onboarding, lifecycle, 운영을 OpenShell과 묶는 reference stack | `RuntimePort`의 교체 지점만 제공 | 계획(P1). 목록 외 agent harness는 공식 Unsupported이므로 이 서비스는 sandbox 안 agent가 호출하는 sandbox 밖 API로 설계(P1-007). 실행 not_run |
| OpenShell | 파일, 네트워크, 프로세스 등 OS 수준 권한을 정책으로 제한하는 security runtime | P0 `LocalRuntime`/`LocalPolicy`가 대신하지 않음 | OpenShell 단독(local standalone)에서 stand-in 역할별 허용·차단을 실제 관측(P1-007C, [증거](evidence/openshell.md)). RFA 제품 경로의 강제 증거는 없음(계획 P1) |

## 모듈별 매핑

### Module 1 — agent loop, model/tool 호출, state, 종료 조건

공식 과정은 observation-action loop에 model call, state, tool, 명확한 stop condition을 더한다. Tool call은 모델의 구조화된 요청이며, harness가 이름과 인자를 검증하고 코드를 실행한 뒤 결과를 다음 호출에 전달한다. Tool schema는 그 자체로 실행이나 안전을 보장하지 않는다.

| 구현 연결 | 파일 | 상태 | 실행 증거 |
| --- | --- | --- | --- |
| 요청 상태와 종료 상태를 가진 Supervisor graph | `src/rfa_mas/application/graphs/supervisor.py` | 실제(local), 검증 완료(P0) | `uv run pytest tests/test_graph.py tests/test_supervisor_boundaries.py -q` — 통과 |
| 두 도메인이 공유하는 TaskGraph template과 제한된 domain 설정 | `src/rfa_mas/application/graphs/domain.py` | 실제(local), 검증 완료(P0) | 두 도메인 graph test와 demo — 통과 |
| 모델과 tool 실행의 명시적 비동기 계약 | `src/rfa_mas/ports/*` | 실제(local) 계약, 검증 완료(P0) | contract/HTTP adapter tests — 통과 |
| 모델 응답과 tool 결과 및 실패 시나리오 | `src/rfa_mas/adapters/mock.py` | 모의(P0) | success/근거 부족/거절/수정/timeout tests — 통과 |
| step/tool/timeout 예산에 따른 종료 | `supervisor.py`, `domain.py` | 실제(local), 검증 완료(P0) | budget/partial-result/timeout tests — 통과 |

실제 NVIDIA 모델의 tool calling 및 구조화 출력 지원은 선택한 모델의 공식 model card로 확인해야 한다. 2026-09-26 hosted `nvidia/nemotron-3.5-lightning-30b-a3b`에서 json_object 응답과 named tool_choice 호출 제안을 합성 요청으로 관측했다([증거](evidence/nvidia-model.md)). 이는 도구 실행이나 제품 graph 경로 검증이 아니다. 모델 ID가 비어 있는 P0 mock 결과를 NVIDIA 호출 증거로 사용하지 않는다.

### Module 2 — routing, 병렬 작업, 자체 자료 RAG, 계획

공식 과정은 workflow를 model call, 결정적 code step, tool call, specialist loop로 이루어진 graph로 설명한다. 정해진 enum 밖의 route는 추측하지 않고 거절하거나 명시적으로 재시도해야 한다. RAG는 사전 index build와 live query/retrieve/generate 경계를 나누며, MCP는 외부 context/tool 호출 경계일 뿐 검색 시점, 결과 검증, task state 조합을 대신하지 않는다. Fresh worker context도 OS sandbox가 아니다.

| 구현 연결 | 파일 | 상태 | 실행 증거 |
| --- | --- | --- | --- |
| Supervisor의 고정 domain routing과 잘못된 route 거절 | `src/rfa_mas/application/graphs/supervisor.py` | 실제(local), 검증 완료(P0) | routing/invalid-domain tests — 통과 |
| 동일 TaskGraph를 prompt, 자료 scope, capability로 구분 | `src/rfa_mas/application/graphs/domain.py` | 실제(local), 검증 완료(P0) | two-domain template test — 통과 |
| 검색 결과를 `EvidenceBundle`로 한정하는 `RetrievalPort` | `src/rfa_mas/ports/*` | 실제(local) 계약, 검증 완료(P0) | DTO/graph contract tests — 통과 |
| 합성 fixture 검색, 근거 부족과 timeout | `src/rfa_mas/adapters/mock.py` | 모의(P0) | evidence/timeout tests와 scenario demo — 통과 |
| 제품 worker의 NeMo Retriever 연결, 병렬 fan-out, deep planning | 교체 adapter 및 worker 미구현 | 계획(P1) | 미실행. 공식 Skill CLI 자체는 P1-003A에서 direct live 확인 |

P0의 단일 domain 위임이나 mock 검색을 병렬 multi-agent 또는 실제 RAG 서비스 검증으로 보고하지 않는다.

### Module 3 — NemoClaw 환경의 실제 agent 실행과 지속 운영

과정은 역할을 LLM endpoint, agent harness, OpenShell sandbox, NemoClaw blueprint로 나눈다. LLM endpoint는 agent session을 보존하지 않고, harness가 session/workspace/tools를 소유하며, OpenShell이 실행 프로세스의 도달 범위를 제한한다. 공식 DLI 저장소는 과정과 검증 도구를 제공할 뿐 NemoClaw와 그 runtime은 외부 dependency라고 명시한다.

| 구현 연결 | 파일 | 상태 | 실행 증거 |
| --- | --- | --- | --- |
| `AgentSpec` 기반 실행/상태/취소 계약 | `src/rfa_mas/ports/*` | 실제(local) 계약, 검증 완료(P0) | runtime contract/idempotency tests — 통과; local cancel은 명시적 `not_implemented` |
| 같은 프로세스에서 task를 실행하는 `LocalRuntime` | `src/rfa_mas/adapters/local.py` | 실제(local), sandbox 아님, 검증 완료(P0) | local runtime 동시 중복·status tests — 통과 |
| runtime timeout/실패 시나리오 | `src/rfa_mas/adapters/http.py`, `application/service.py` | 모의(P0) | 부작용 timeout의 `outcome_unknown` 보존 test — 통과 |
| NemoClaw sandbox agent가 이 서비스의 제한 API를 호출 | 설계 [NemoClaw 증거](evidence/nemoclaw.md) §3~§7; runtime adapter 미구현 | 계획(P1-007A) | 미실행. NemoClaw quickstart도 미실행(inference credential·자원 blocker) |
| 임의 LangGraph/FastAPI 서비스를 NemoClaw agent runtime으로 수용 | 공식 Platform Support에서 목록 외 harness Unsupported(2026-09-27 확인) | 공식 미지원 | 이 서비스는 sandbox 밖 host 프로세스로 둔다 |

NemoClaw 공식 overview의 현재 예시는 OpenClaw, Hermes, LangChain Deep Agents Code용 agent-specific integration이다. Platform Support는 그 밖의 LangChain·AutoGen·CrewAI 및 목록에 없는 agent harness를 Unsupported로 명시한다. 이것을 이 저장소의 임의 서비스 지원으로 일반화하지 않는다.

### Module 4 — OpenShell을 통한 tool, 파일, 네트워크 통제

과정은 prompt가 의도를 유도하는 계층, harness가 요청을 검증하는 계층, sandbox가 OS 권한을 강제하는 계층을 분리한다. OpenShell의 강제는 실제 policy와 실행 관찰로 확인해야 하며, command 실패 하나만으로 어떤 정책이 차단했다고 단정할 수 없다.

| 구현 연결 | 파일 | 상태 | 실행 증거 |
| --- | --- | --- | --- |
| 검색·공유·tool 요청 전 결정적 정책 판단 | `src/rfa_mas/ports/*`, `src/rfa_mas/adapters/local.py` | 실제(local) application policy, 검증 완료(P0) | `uv run pytest tests/test_policy.py -q` — 통과 |
| membership와 audience를 결합한 접근 거절 | `src/rfa_mas/adapters/local.py` | 실제(local), 검증 완료(P0) | owner/business-unit/company/public tests — 통과 |
| public DRAFT의 private privacy canary 차단 | `domain.py`, `tests/test_graph.py` | 실제(local), 검증 완료(P0) | public canary non-disclosure test와 demo — 통과 |
| OpenShell filesystem/network/process 강제와 credential delivery | RFA runtime adapter 미구현; OpenShell 단독 관측은 [OpenShell 증거](evidence/openshell.md)(P1-007C 역할별 matrix)와 [NemoClaw 증거 §8](evidence/nemoclaw.md) | OpenShell 단독 real(합성, stand-in 역할 정책), RFA 경로 계획(P1-007B) | 역할별 파일·네트워크·실행 허용/차단을 OCSF 기록 또는 policy differential로 판정. 파일 부재·Unix 권한만으로 설명되는 실패(`/etc` 쓰기 등)는 inconclusive로 두고 정책 거부로 세지 않음. credential delivery와 RFA 제품 역할 실행 경로는 미실행 |

`LocalPolicy`의 거절은 애플리케이션 정책 테스트이며 OS sandbox 증거가 아니다. LLM 검수나 mock review도 결정적 접근 정책을 완화하지 않는다.

## NVIDIA Skill — NeMo Retriever

확인 시점의 공식 `nemo-retriever` skill은 NeMo Retriever **26.8.1 CLI** 사용 지침이다. 로컬 LanceDB index 또는 이미 배포된 Retriever service에 대해 `retriever ingest`와 `retriever query ... --format evidence`를 사용하고, 근거만으로 답하며 source/page metadata를 보존하라고 지시한다. Service 인증이 필요할 때의 bearer token 경계도 설명한다.

| 구현 연결 | 파일 | 상태 | 실행 증거 |
| --- | --- | --- | --- |
| 검색 입력/출력의 안정된 application 계약 | `src/rfa_mas/ports/*` | 실제(local) 계약, 검증 완료(P0) | contract/graph tests — 통과 |
| 합성 근거를 반환하는 검색 backend | `src/rfa_mas/adapters/mock.py` | 모의(P0) | graph/evidence tests — 통과 |
| 공식 Skill 절차의 CLI ingest/query 실제 실행 | `tests/integration/test_retriever_live.py`, 저장소 밖 pinned nemo-retriever==26.8.1 | 실제(hosted embedding), 검증 완료(P1-003A direct smoke) | [증거](evidence/nvidia-skill.md) — 합성 PDF 3질문 top-1 source/page 일치 |
| Skill 지침을 따르는 worker와 제한된 CLI/service adapter | 미구현 | 계획(P1) | CLI/service 결과를 `EvidenceBundle`로 정규화하는 test 필요 |

`SKILL.md`는 사용 절차이지 LangGraph node나 tool adapter 구현이 아니다. Skill 설치만으로 graph 연결, index 준비, service 배포, 권한 검증이 끝났다고 주장하지 않는다. `26.8.1`은 확인 시점의 공식 skill 값이므로 P1 착수 때 다시 확인하고 호환 버전을 고정한다. 2026-09-26 재확인 결과 여전히 26.8.1이며 build.nvidia.com Skills 카탈로그에 등재되어 있다.

## LangGraph

공식 문서는 LangGraph를 long-running, stateful agent를 위한 low-level orchestration framework/runtime으로 설명한다. 결정적 step과 LLM-driven step을 한 graph에서 혼합할 수 있지만 prompt, application architecture, model/tool integration을 대신 정의하지 않는다. Persistence, human-in-the-loop, memory 기능의 존재도 이 프로젝트가 그것을 모두 구현했다는 뜻은 아니다.

| 구현 연결 | 파일 | 상태 | 실행 증거 |
| --- | --- | --- | --- |
| Supervisor graph와 공통 Domain TaskGraph 구성 | `src/rfa_mas/application/graphs/supervisor.py`, `domain.py` | 실제(local), 검증 완료(P0) | graph/supervisor boundary tests — 통과 |
| graph node가 port만 호출하고 adapter는 composition root에서 주입 | `src/rfa_mas/application/graphs/*`, `src/rfa_mas/ports/*` | 실제(local) 설계, 검증 완료(P0) | import dependency-boundary test — 통과 |
| 외부 승인과 runtime 상태를 graph checkpoint와 직접 공유하지 않음 | port/DTO 경계 | 실제(local) 설계, 검증 완료(P0) | SQLite schema 및 contract review — 통과 |
| 로컬 영속 checkpointer·승인 대기 재개 | 미구현, P0-016/021 | 확장된 P0 계획 | 미실행 |
| live human-in-the-loop, distributed deployment | 미구현 | 실제 승인은 P1-008A; 분산 배포는 4일 범위 밖 | 미실행 |

LangGraph checkpoint는 중단된 graph 실행을 재개하기 위한 상태이고, 장기 KB는 출처·버전·audience를 가진 지식 저장소다. 둘을 같은 저장 책임으로 취급하거나 팀원 서비스 DB와 직접 공유하지 않는다.

## 통합 주장 제한

- `MockModel`, mock retrieval, mock response, mock tool 결과는 `simulated=true`와 adapter 식별자를 유지한다.
- `LocalRuntime`과 논리 namespace 분리는 OpenShell sandbox, process isolation 또는 권한 강제가 아니다.
- `LocalPolicy`와 privacy canary 테스트는 애플리케이션의 공개 범위 검사이며 NemoClaw/OpenShell 검증이 아니다.
- Skill 설치 또는 파일 존재는 tool 연결, 자동 선택, CLI 실행, index 생성의 증거가 아니다.
- NemoClaw가 이 LangGraph/FastAPI 서비스를 자동으로 sandbox에 넣는다고 가정하지 않는다.
- OpenShell 단독 sandbox의 관측(P1-007C stand-in 역할 정책 포함)은 RFA 서비스 경로, NemoClaw 경로, RFA 제품 역할 실행의 권한 강제 증거가 아니다. RFA backend는 sandbox 밖에서 실행되며 보호되지 않는 범위는 [NemoClaw 증거 §6](evidence/nemoclaw.md)에 있다.
- 실제 NemoClaw/OpenShell 주장은 공식 platform support 확인, live policy, runtime identity, 허용/차단 실행 증거가 모두 있을 때만 갱신한다.
- 실제 모델, Retriever, MCP, 팀원 Response/Runtime 서비스가 없으면 명확한 `not_implemented` 또는 `configuration_error`를 반환하며 mock으로 조용히 대체하지 않는다.
- NemoClaw 공식 문서는 현재 제품을 trusted operator가 사용하는 one-host early-preview reference stack으로 한정한다. 이를 hosted service, multi-tenant enterprise control plane 또는 enterprise identity system으로 설명하지 않는다.

## 공식 출처

확인일: 2026-09-25 (KST). 버전과 지원 범위는 P1 통합 직전에 다시 확인한다.

- [NVIDIA DLI 과정 홈](https://nvdli.github.io/NemoClawDLI/nemoclaw/)
- [DLI 공식 저장소와 모듈 요약](https://github.com/NVDLI/NemoClawDLI)
- [Module 1a: agent loop](https://nvdli.github.io/NemoClawDLI/nemoclaw/01a-loop.html)
- [Module 1b: ReAct loop](https://nvdli.github.io/NemoClawDLI/nemoclaw/01b-react.html)
- [Module 1c: tools와 MCP](https://nvdli.github.io/NemoClawDLI/nemoclaw/01c-tools.html)
- [Module 2a: routing과 parallel workflow](https://nvdli.github.io/NemoClawDLI/nemoclaw/02a-routing.html)
- [Module 2b: RAG](https://nvdli.github.io/NemoClawDLI/nemoclaw/02b-rag.html)
- [Module 2c: deep agents](https://nvdli.github.io/NemoClawDLI/nemoclaw/02c-deep.html)
- [Module 3a: NemoClaw stack 연결](https://nvdli.github.io/NemoClawDLI/nemoclaw/03a-kickstart.html)
- [Module 3b: OpenClaw workspace와 persistence](https://nvdli.github.io/NemoClawDLI/nemoclaw/03b-openclaw.html)
- [Module 3c: skill과 always-on 실행](https://nvdli.github.io/NemoClawDLI/nemoclaw/03c-always-on.html)
- [Module 4a: OpenShell sandbox 경계](https://nvdli.github.io/NemoClawDLI/nemoclaw/04a-safety.html)
- [NVIDIA NeMo Retriever Skill](https://github.com/NVIDIA/skills/blob/main/skills/nemo-retriever/SKILL.md)
- [NVIDIA NemoClaw overview](https://docs.nvidia.com/nemoclaw/latest/about/overview.html)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)

P1-007 추가 확인일: 2026-09-27 (KST). 상세 목록은 [NemoClaw 증거의 공식 출처](evidence/nemoclaw.md#공식-출처)에 있다.

- [NVIDIA DLI 과정 페이지](https://learn.nvidia.com/courses/course-detail?course_id=course-v1:DLI+S-FX-43+V1)(HTTP 200, 본문 미확인)
- [과정 제목·학습 목표 canonical](https://github.com/NVDLI/NemoClawDLI/blob/main/web/nemoclaw/COURSE_CANON.md)
- [NemoClaw Platform Support](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/reference/platform-support)
- [OpenShell Support Matrix](https://docs.nvidia.com/openshell/about/support-matrix)
