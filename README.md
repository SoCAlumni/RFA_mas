# RFA MAS — NemoClaw 보안 그룹 운영층

사내 비서 에이전트 여러 개를 **보안팀이 승인할 수 있는 형태**로 운영하는 NemoClaw/OpenShell 기반 멀티 에이전트 시스템.

- **위협**은 외부 공격자가 아니라 내 에이전트 자신이다. 프롬프트 인젝션이나 오작동으로 사내 정보가 외부 모델이나 외부 목적지로 샐 수 있다.
- **보안 주장**: 기밀 영역을 나가는 것은 자동 검열(regex + LLM)을 통과한 텍스트뿐이다. 사람 결재가 게시 직전에 한 번 더 검사하고, 거절 사유는 검열 규칙으로 되먹임된다.
- **역할 분담**: 이 저장소는 지식 서버·채팅·태스크 팀·샌드박스·검열·감사·결재함 API를 맡는다. desk, 결재 서버, 게시는 RFA_module(상대 팀)이 맡는다.

## 핵심 설계

1. **보안 그룹 = OpenShell preset.** 샌드박스는 그룹의 조합이다. 기본 샌드박스 `rfa-main` 하나에 모든 에이전트를 두고, 격리가 필요한 에이전트만 `sandbox:`를 지정해 옮긴다.
2. **egress-proxy가 유일한 추론 경로.** 모든 샌드박스의 `inference.local`이 호스트 프록시를 가리킨다. 프록시는 HMAC 마커로 채널과 에이전트를 귀속하고, 채널에 따라 검열(redact/block)한 뒤 hosted 모델(build.nvidia.com Nemotron, 선택 시 Gemini)로 보낸다. 로컬 LLM은 쓰지 않는다.
3. **`ask()` 한 함수.** head(담당 선택) → 브로커(샌드박스 안 task 에이전트) → 검열(audience 프로파일). 채팅, desk `/ask`, RFA_module `/v1/head/ask`가 모두 이 함수를 쓴다.
4. **태스크 = 팀.** supervisor 하나와 승인된 역할 카탈로그 안의 멤버(research·benchmark·summarizer·verifier)로 구성한다. 멤버는 supervisor만 spawn할 수 있고, verifier가 근거와 대조한다.
5. **선언형.** `deploy/nemoclaw/*.yaml`을 바꾸면 컨트롤러가 NemoClaw CLI로 reconcile한다.

흐름·에이전트 호출/생성·보안 상세는 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** 에 있다.

## 실행

```bash
make bootstrap        # 검증 → serve(프록시·브로커·진입점) → KB 시드 → knowledge facade → 샌드박스 온보딩·reconcile
make serve            # 호스트 서비스만 (serve-stop 으로 정지)
make plan && make apply   # 선언 변경 → NemoClaw 반영 (tools·subagents 동기화 포함)
make test             # 운영층 단위 테스트
make mock-e2e         # desk/결재 목업으로 /ask 시나리오 4개 (샌드박스·모델 없이)
make demo             # demo/01..10 (DEMO_MODE=live 로 라이브)
make logs-trace REQUEST_ID=…   # 요청 하나의 전 구간 로그
make teardown
```

`.env.dev`(0600, git-ignore)에 `NVIDIA_API_KEY`, `RFA_ASK_TOKEN`, `RFA_LLM_PROVIDER=nvidia|gemini`를 넣는다. 변수 목록은 `.env.example`에 있다.

## API (진입점 `127.0.0.1:8799`)

| 사용자 | API |
| --- | --- |
| 프런트 | `GET /me` · `GET /agents` · `GET/POST /tasks`(SSE) · `POST /chat`(SSE) · `/conversations` · `/inbox*` · `/tasks/{id}/sources` · `/admin/agents*` · `/admin/sandboxes*` |
| RFA_module desk | `POST /v1/head/ask` (loopback) · `POST /ask` (bearer) |
| 운영 | `/teams` · `/chat/sync` · `/audit/`(감사 화면) · `/docs` |

Bearer `RFA_ASK_TOKEN`이 맞으면 owner, 없거나 틀리면 guest로 처리한다. 명세는 [docs/openapi.yaml](docs/openapi.yaml)(`make openapi`)에 있고, 화면별 사용법은 [docs/FE_API_GUIDE.md](docs/FE_API_GUIDE.md)에 있다.

## 저장소 구조

| 경로 | 내용 |
| --- | --- |
| `src/rfa_mas/nemoclaw/` | 운영층: 진입점(routes/services/store/schemas), `ask`, 브로커, 프록시, 검열, 컨트롤러 |
| `deploy/nemoclaw/` | 선언: assignments · routing · censors · ask · roles · teams · frontend, preset·skill·KB |
| `src/rfa_mas/{application,ports,adapters,api,knowledge_facade}` | 코어층(LangGraph 제품 코어, 사내 지식 API :8795) |
| `tools/mock/`, `demo/` | 상대 팀 목업, 데모 스크립트 |
| `tasks/`, `TASKS.md`, `scripts/tasklib/` | 개발 task 관리(taskctl) |
| `legacy/` | 보류된 admission queue 등 |

## 문서

| 문서 | 내용 |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 기술 키워드, 실제 흐름, 에이전트 호출·생성, 보안 상세 |
| [docs/FE_API_GUIDE.md](docs/FE_API_GUIDE.md) · [docs/openapi.yaml](docs/openapi.yaml) · [docs/api/](docs/api/) | 프런트 연동 가이드, API 계약 |
| [docs/decisions.md](docs/decisions.md) | 설계 결정 D-0.1 ~ D-24 |
| [docs/WORK_LOG.md](docs/WORK_LOG.md) | 작업 기록 (SG-N, FE-N) |
| [docs/evidence/](docs/evidence/) | NVIDIA 모델·Skill·OpenShell·NemoClaw 실행 증거 |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) · [docs/PRODUCT_REQUIREMENTS.md](docs/PRODUCT_REQUIREMENTS.md) · [RFA_E2E_Test_Scenarios_10_ko.md](RFA_E2E_Test_Scenarios_10_ko.md) | 코어층 계약, 요구사항, 인수 시나리오 |
| [AGENTS.md](AGENTS.md) · [TASK_EXECUTION_RULES.md](TASK_EXECUTION_RULES.md) · [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) | 에이전트 작업 규칙 (taskctl) |

## 검증 상태 (2026-09-28)

- `make test`: 89 passed, 2 failed. 실패한 2개는 fail-closed를 단언하는 검열 테스트로, D-0.3 결정(판정 실패 시 regex 결과로 통과)과 충돌해 사용자 결정을 기다리는 중이다. `make mock-e2e`는 4/4.
- 라이브(`rfa-main`, hosted lightning): `/chat` owner 약 15초(head 1.7초 + research 턴 13초). 팀 `/ask`는 NPU 36초, Inference 24초로, supervisor → 멤버 facade 조회 → verifier revise/pass를 거쳐 답한다.
- RFA_module desk를 `/v1/head/ask`로 연동했다: 접수 → 초안 → 결재 → 재생성(round 2) → 응답(mock 게시).
- 알려진 한계: `POST /teams`·`POST /tasks`에는 tools·subagents 동기화가 없다(`make apply` 필요). Colima 8 GiB에서는 샌드박스 1개가 실용적인 한도다.

## 코어층 빠른 시작

```bash
uv sync --locked --extra nat
uv run rfa demo                       # 키 없이 mock/local
uv run --extra nat python -m pytest -q   # pytest 실행 파일 대신 python -m pytest
```
