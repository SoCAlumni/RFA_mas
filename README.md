# RFA MAS

NVIDIA Korea Agentic AI Hackathon 온라인 사전 챌린지를 위한 기업형 개인 비서다. 비서 Supervisor가 요청을 도메인 TaskGraph나 작업 팀에 위임하고, 권한을 확인한 근거로만 DRAFT를 만들며, 검토·승인을 거친 뒤에만 게시 단계로 넘어간다. 모든 외부 경계는 port/adapter로 분리되어 mock, 로컬 stand-in, 실제 서비스를 설정만으로 바꾼다. SRNote의 노트 저장·탐색·지식 축적 개념을 참고했다.

이 문서는 `wip/stack` 260f394 기준이며 2026-09-27 KST에 실제로 실행한 결과만 적는다. mock·로컬 stand-in의 성공은 NVIDIA, MCP, OpenShell, 팀원 서비스, 외부 게시의 실제 결과가 아니다.

## 빠른 시작

필수 환경은 Python 3.12와 [uv](https://docs.astral.sh/uv/)다. 패키지 버전은 `uv.lock`에 고정되어 있다.

```bash
uv sync --locked      # 기본(mock/local) 설치. 모델 API key, GPU, Docker가 필요 없다
uv run rfa doctor     # 설정 변수 이름과 configured/missing 상태만 출력
uv run rfa demo       # 합성 TRIV3 공개 DRAFT, simulated=true
```

`.env`가 없어도 기본 mock/local 모드로 동작한다. 특정 설정 파일은 global option으로 subcommand 앞에 지정한다: `uv run rfa --env-file <파일> <command>`. 파일이 없거나 symlink·비정규 파일이면 기본값으로 fallback하지 않고 거절한다.

전체 테스트 환경은 NVIDIA Agent Toolkit(NAT) extra를 포함한다.

```bash
uv sync --locked --extra nat
uv run --extra nat python -m pytest -q
```

`python -m pytest`로 실행한다. `pytest` 실행 파일을 직접 쓰면 저장소 루트가 import 경로에 없어 `scripts.contract_baseline`을 import하는 test가 수집 단계에서 실패한다(2026-09-27 관측). extra 없이 `uv sync --locked`를 실행하면 환경이 기본 설치로 돌아가 NAT extra 패키지가 제거된다(2026-09-27 관측). control-root 전용 `tests/test_task_migration.py`, `tests/test_taskctl.py`는 작업용 worktree 사본을 거절하므로 worktree에서는 `--ignore`로 제외한다.

통합 E2E 시나리오(E2E-01~10) 결과와 `rfa demo --full`은 P0-026이 작성하는 [docs/DEMO.md](docs/DEMO.md)를 본다. 이 branch 기준으로 DEMO.md는 아직 통합되지 않았다.

## 명령

아래는 2026-09-27 KST에 격리된 임시 데이터 디렉터리에서 실행해 확인한 명령이다. 설정은 `DATABASE_URL`, `TRACE_DIR`, `LOG_LEVEL` 등만 적은 합성 env 파일을 `--env-file`로 넘겼다.

| 명령 | 용도 | 관측 결과 |
| --- | --- | --- |
| `uv run rfa doctor` | 선택 mode 점검 | `ready=true`, missing 없음, external write/egress effective `false`. 값·일부 문자열·길이는 출력하지 않는다 |
| `uv run rfa demo` | TRIV3 공개 DRAFT | `completed`, `simulated=true`, 게시 `not_requested` |
| `uv run rfa demo --domain quantization_research --audience public` | 두 번째 도메인 | 위와 같음 |
| `uv run rfa demo --scenario insufficient_evidence` (`revision_requested`, `timeout`도 같음) | 결정적 검토 시나리오 | `waiting_approval`, exit 0 |
| `uv run rfa demo --scenario policy_denied` | 권한 거절 | `failed`/`policy_denied`, DRAFT 없음, exit 1(의도한 실패) |
| `uv run rfa api` | FastAPI, 기본 `127.0.0.1:8000` | `/healthz` 200, `/readyz` `ready`, `POST /v1/work` 201 `completed`, `GET /v1/work/{run_id}`와 `GET /v1/runs/{run_id}/status` 200, `/openapi.json` 200 |
| `uv run rfa scheduler --run-seconds 3` (`SCHEDULER_ENABLED=true`) | API와 별도 프로세스인 단일 owner 예약 runner | `scheduler_running`, exit 0. `SCHEDULER_ENABLED`가 false면 exit 2 `scheduler_disabled` |
| `uv run rfa evaluate --dataset persona-regression-v2 --label baseline --output <새 파일>` | 합성 persona 회귀(simulated) | 24/24 실행, 23 pass, 1 fail, 보안 실패 0, release gate `fail` → exit 1. 격리를 위해 `--env-file`은 거절된다 |
| `uv run rfa evaluate-compare --baseline <A> --candidate <B> --output <새 파일>` | 같은 조건의 두 실행 비교 | `comparable=true`, 24/24 unchanged, 보안 회귀 0, exit 1(release `fail` 유지) |
| `uv run rfa openapi --output <경로>` | OpenAPI export | 41개 path |
| `uv run rfa init-env --output .env.dev` | 서비스별 로컬 내부 credential profile 생성 | 8개 변수 생성, 파일 권한 0600, 변수 이름만 출력. `NVIDIA_API_KEY`는 만들지 않는다. 저장소 루트의 `.env` 또는 git-ignore된 `.env.<profile>`만 허용한다 |

`doctor`와 `demo`는 `uv run`으로, 나머지는 같은 환경의 `rfa` console script로 실행했다. 시나리오 demo는 결정적 P0 시나리오이며 실제 외부 장애나 OpenShell 차단 증거가 아니다.

loopback 기본 모드의 작업 요청 예:

```bash
curl -sS http://127.0.0.1:8000/v1/work \
  -H 'Content-Type: application/json' \
  -d '{"query": "TRIV3 공개 트랙을 근거와 함께 요약해 줘.", "domain_id": "triv3", "target": {"audience": "public"}}'
```

`APP_HOST`를 loopback 밖으로 바꾸면 `APP_API_KEY`가 필수이고 작업 API에 `Authorization: Bearer`가 필요하다. 로컬 identity는 실사용 인증이 아니다. 전체 route는 생성된 OpenAPI(`GET /docs`, `GET /openapi.json`)가 기준이다.

## 옵트인 live 검증

기본 suite에서 live test는 skip되며 skip은 증거가 아니다. credential은 명시한 env 파일에서만 읽고 출력·저장하지 않는다. `NVIDIA_API_KEY`는 `.env.dev` 같은 비공개 profile에 직접 넣는다.

2026-09-27 KST에 아래 두 명령을 합성 자료로 각 1회 실제 실행했다(n=1).

```bash
# P1-002: 제품 ModelPort 경로 -> hosted NVIDIA chat completions
RFA_NVIDIA_LIVE=1 RFA_NVIDIA_ENV_FILE=/abs/path/.env.dev \
RFA_NVIDIA_PRODUCT_EVIDENCE_OUT=/tmp/p1002-product-live.json \
  .venv/bin/python -m pytest -q tests/integration/test_nvidia_product_live.py

# P1-003: 제품 Research 경로 -> 공식 nemo-retriever Skill CLI 26.8.1(hosted embedding)
RFA_RETRIEVER_PRODUCT_LIVE=1 RFA_NVIDIA_ENV_FILE=/abs/path/.env.dev \
RFA_RETRIEVER_BIN=/abs/path/nemo-retriever-26.8.1/.venv/bin/retriever \
RFA_RETRIEVER_PRODUCT_EVIDENCE_OUT=/tmp/p1003-product-live.json \
  .venv/bin/python -m pytest -q tests/integration/test_retriever_product_live.py
```

| 실행 | 결과(n=1) |
| --- | --- |
| P1-002 제품 모델 경로 | 1 passed. HTTP 시도 1회, 모델 호출 29.8초, Run 30.3초, `completed`. 합성 공개 노트만 전송했고 owner 전용 노트·canary는 보내지 않았다. [모델 증거](docs/evidence/nvidia-model.md) |
| P1-003 제품 Research 경로 | 4 passed(11.25초). 합성 PDF 2개 ingest 7.17초, Research run 3.9초, Skill 결과 3개 반환·3개 사용·0개 unmapped, 관측 mode `real`. 측정 JSON은 임시 경로에만 있고 저장소 evidence 문서(`docs/evidence/nvidia-skill.md`의 P1-003 절)는 아직 갱신되지 않았다 |

다른 opt-in live test는 이번 문서 작업에서 다시 실행하지 않았다. 조건과 결과는 각 evidence 문서에 있다: `tests/integration/test_nvidia_live.py`(P1-002A, [모델 증거](docs/evidence/nvidia-model.md)), `tests/integration/test_retriever_live.py`(P1-003A, [Skill 증거](docs/evidence/nvidia-skill.md)), `tests/integration/test_langfuse_live.py`(P1-006C, 로컬 self-host Langfuse 필요, [LLMOps 증거](docs/evidence/llmops.md)), `tests/integration/test_openshell_live.py`(P1-007C, OpenShell gateway와 rootfs 필요, [OpenShell 증거](docs/evidence/openshell.md)).

## 아키텍처

```text
client / 로컬 UI (P0-025A)
  -> FastAPI (api/app.py): 설치 owner 인증, 도메인 오류 -> HTTP 변환
    -> application: WorkService, Supervisor graph, 공통 Domain TaskGraph, TeamRunner,
       DraftLifecycle, 세션/KB/예약/피드백 서비스, 효과 ledger
      -> ports (async Protocol): Model, Retrieval, Response, Tool, Runtime, Policy,
         Trace, WorkRepository
        -> adapters (bootstrap.py에서만 선택·주입)
           mock.py | local.py (SQLite, LocalRuntime, LocalPolicy, JSONL trace)
           http.py (Response/Publication/Tool/Runtime/Policy reference HTTP)
           nvidia.py | nemo_retriever.py | langfuse.py | scheduler.py | checkpoints.py
```

- 호출 방향은 `API -> application/graph -> port`다. graph node는 URL, 인증 header, MCP SDK, OpenShell CLI를 모르고 mock/real 분기를 하지 않는다.
- 선택한 실제 backend가 준비되지 않으면 `configuration_error` 또는 `not_implemented`로 실패하며 mock으로 fallback하지 않는다.
- cloud 모델에는 public 근거만 보낸다. P1-005 screen이 먼저 거르고, P1-002의 public-only gate가 한 번 더 확인한다.
- 팀원 모듈 자리를 채우는 로컬 stand-in이 있다: P1-008C 검토·게시·READ tool 서비스(`src/rfa_mas/reference/local_response*.py`), P1-008D runtime 서비스(`src/rfa_mas/reference/local_runtime*.py`), P0-025A UI(`src/rfa_mas/ui/`). 교체 방법은 [docs/INTEGRATION.md](docs/INTEGRATION.md)의 "로컬 stand-in 교체"를 본다.

## 실제 vs mock/local

| 구성요소 | 기본 실행(키 없음) | 선택 가능한 실제 경로 | 실제로 실행한 증거 | 아직 아닌 것 |
| --- | --- | --- | --- | --- |
| 모델 | `MockModel`(simulated) | `MODEL_PROVIDER=nvidia` -> `NvidiaChatModel` | hosted Nemotron 합성 호출(P1-002A), 제품 경로 live n=1(P1-002) | 품질·지연 분포, 한국어 golden 평가, 실제 Judge |
| 공식 Skill(NeMo Retriever) | 없음 | `RETRIEVER_BACKEND=nemo_cli` -> Research 팀 source_scout의 Skill 도구 | CLI 26.8.1 direct 실행(P1-003A), 제품 Research 경로 live n=1(P1-003) | `nemo_service`(reserved), local embedding NIM, Skill 결과를 DRAFT 근거로 결합 |
| KB·검색 | `LocalRetrieval`: SQLite, 권한 확인 뒤 한국어 BM25 | 없음 | offline test(real local) | 외부 벡터 검색 |
| 검토·승인 | `MockResponse` | `RESPONSE_BACKEND=http` -> reference fixture 또는 P1-008C stand-in | stand-in 계약 test(local, receipt mode=mock) | 팀원 Response 서비스(P1-008A) |
| 게시 | `MockPublisher`(메모리 sink) | `PublicationHttpAdapter` -> stand-in의 local-artifact receipt | 계약 test(mode=mock) | 실제 외부 게시(P0에서 금지) |
| Tool | 팀 역할은 `LocalAnalysisTools`(READ 계산). `TOOL_BACKEND`의 ToolPort는 Work 경로에서 호출하지 않는다 | `TOOL_BACKEND=http`(reference) | 계약 test | MCP 실행, WRITE |
| Runtime | `LocalRuntime`(process-local) | `RUNTIME_BACKEND=http` -> P1-008D stand-in | 계약 test | OpenShell 위 RFA 역할 실행(P1-007B), 팀원 runtime(P1-008B) |
| OpenShell | 사용 안 함 | 없음 | 로컬 standalone OpenShell v0.1.1에서 stand-in 역할별 허용·차단(P1-007C) | RFA 제품 경로 |
| NemoClaw | 사용 안 함 | 없음 | 설계 문서만(P1-007) | 실행 not_run(P1-007A blocked) |
| Policy | `LocalPolicy`(application policy) | `POLICY_BACKEND=http`(reference) | offline test | OS 수준 강제 |
| Trace | 로컬 JSONL + SQLite 관측 원장 | `TRACE_BACKEND=langfuse` + `LANGFUSE_EXPORT_ENABLED=true`(loopback) | 로컬 self-host Langfuse 4.46.0 export·ID 조회·ID 삭제(P1-006C) | Langfuse 보존 적용(P1-006F blocked), 비loopback |
| 평가 | 규칙 평가(simulated, mock 모델) | NAT 1.8 installed smoke(P0-028, mock 공급자) | persona v2 23 pass/1 fail/보안 0(2026-09-27) | 실제 Judge, actual 모델 평가 |
| 예약 | `SCHEDULER_ENABLED=false` | `rfa scheduler`(APScheduler, 별도 프로세스) | 3초 smoke, offline test | 예약 알림 생산자 |
| UI | 없음 | `rfa_mas.ui.app.create_local_ui_app`(P0-025A) | ASGI test(`tests/test_local_ui.py`) | 실행 CLI. 이 문서 작업에서 브라우저 실행은 하지 않았다 |

## 해커톤 평가 기준 연결

공식 폼(AGENTS.md의 2026-09-24 KST 확인 기록)의 평가 항목에 이 저장소의 task와 증거를 연결한다. 폼은 항목별 배점이나 가중치를 공개하지 않았으므로 점수·가중치를 붙이지 않는다. 온라인 사전 챌린지 접수 마감은 **2026-09-28 23:59 KST**다. 2026-09-27 공개 웹 검색에서는 2차 출처(LinkedIn 게시물)가 같은 접수 기간(2026-09-11~09-28 23:59)을, NVIDIA AI Day Seoul 페이지가 2026-11-10 쇼케이스 일정을 보여 주었고 다른 공지는 찾지 못했다. 공식 폼 자체는 이번에 다시 열어 보지 않았다. 제출 요건은 제출 직전에 공식 폼으로 다시 확인한다.

| 평가 항목 | task | 증거 파일 | 상태 |
| --- | --- | --- | --- |
| NVIDIA Agent 기술 활용 심도: 실제 모델 | P1-002A, P1-002 | [nvidia-model.md](docs/evidence/nvidia-model.md) | real: hosted Nemotron 합성 호출, 제품 경로 n=1 |
| NVIDIA Agent 기술 활용 심도: Skill | P1-003A, P1-003 | [nvidia-skill.md](docs/evidence/nvidia-skill.md), `tests/integration/test_retriever_product_live.py` | real: 공식 CLI direct 실행, 제품 Research 경로 n=1(측정 기록 미커밋) |
| NVIDIA Agent 기술 활용 심도: OpenShell | P1-007C, P1-007B | [openshell.md](docs/evidence/openshell.md) | real(로컬 standalone, stand-in 역할 정책). RFA 제품 경로 not_run |
| NVIDIA Agent 기술 활용 심도: NemoClaw | P1-007, P1-007A | [nemoclaw.md](docs/evidence/nemoclaw.md) | 설계만. 실행 not_run(P1-007A blocked) |
| NVIDIA Agent 기술 활용 심도: Agent Toolkit(NAT) | P0-027, P0-028 | [NAT_COMPATIBILITY.md](docs/NAT_COMPATIBILITY.md), `tests/test_nat_smoke.py` | NAT 1.8.0 installed offline smoke(mock 공급자). live 모델이나 runtime 격리 증거가 아님 |
| 실용성·산업 가치·혁신성 | P0-015, P0-020, P0-022~024, P1-001A/D, P1-005/005A/005B, P1-008 | [INTEGRATION.md](docs/INTEGRATION.md), [PRODUCT_REQUIREMENTS.md](docs/PRODUCT_REQUIREMENTS.md) | 권한 인지 비서, 근거 제한 DRAFT, 검토 후 게시, 예약, 피드백이 real(local)+mock으로 동작. 팀원 실서비스 미연결 |
| 완성도 | P0-021, P1-006B, P0-026, P1-009 | 이 README의 "검증 상태", [llmops.md](docs/evidence/llmops.md), [DEMO.md](docs/DEMO.md)(P0-026) | offline suite 통과(아래). persona v2 release gate `fail`(기능 실패 1). E2E-01~10은 P0-026 작성 중 |
| 커스터마이징·독창성 | P1-004, P1-005, P0-021, P1-001D, P1-006E | [INTEGRATION.md](docs/INTEGRATION.md), [llmops.md](docs/evidence/llmops.md), `tests/test_redteam_regression.py` | 두 도메인이 공유하는 TaskGraph template, audience 라벨과 membership을 결합한 정책, cloud egress screen, 효과 ledger, 한국어 BM25, 레드팀 회귀 |

## 설정과 key 관리

`.env.example`가 설정 이름의 기준이며 `src/rfa_mas/settings.py`와 1:1로 대응한다. 실제 `.env`, 환경별 secret 파일, `.local/`, DB, 로그, 가상환경은 `.gitignore` 대상이다.

| Port | 서비스 | 설정 |
| ---: | --- | --- |
| 8000 | RFA Supervisor API | `APP_HOST`, `APP_PORT`, `APP_API_KEY` |
| 8001 | 통합 reference fixture | 테스트 전용 |
| 8011 | Response/Review | `RESPONSE_BASE_URL`, `RESPONSE_API_TOKEN` |
| 8012 | MCP/Tool gateway | `TOOL_BASE_URL`, `TOOL_API_TOKEN` |
| 8013 | Agent Runtime | `RUNTIME_BASE_URL`, `RUNTIME_API_TOKEN` |
| 8014 | Policy decision | `POLICY_BASE_URL`, `POLICY_API_TOKEN` |
| 7670 | NeMo Retriever service boundary | `RETRIEVER_SERVICE_URL`, `NEMO_RETRIEVER_API_TOKEN` |
| 3000 | Self-hosted Langfuse(opt-in, loopback만) | `LANGFUSE_BASE_URL`, Langfuse key pair |

| 선택 | 직접 입력할 변수 | 현재 동작 |
| --- | --- | --- |
| 기본 loopback mock/local | 없음 | 기본값 |
| `APP_HOST`를 loopback 밖으로 변경 | `APP_API_KEY` | 작업 API에 bearer 인증 필요 |
| `MODEL_PROVIDER=nvidia` | `NVIDIA_MODEL`, `NVIDIA_API_KEY`(선택: `NVIDIA_BASE_URL`, 기본 hosted endpoint; `NVIDIA_MAX_OUTPUT_TOKENS`, 기본 1024) | 실제 adapter. 누락·잘못된 값은 해당 변수 이름의 `configuration_error`이며 mock으로 대체하지 않는다 |
| `RETRIEVER_BACKEND=nemo_cli` | `RETRIEVER_CLI_PATH`, `NVIDIA_API_KEY`, `RETRIEVER_INDEX_DIR`의 `index_manifest.json` | Research 팀의 Skill 도구. KB 검색은 로컬로 남는다. 비공개 표식이 있는 질의는 hosted embedding으로 보내지 않는다 |
| `RETRIEVER_BACKEND=nemo_service` | `RETRIEVER_SERVICE_URL`, `NEMO_RETRIEVER_API_TOKEN` | reserved: `not_implemented` |
| `RESPONSE_BACKEND=http` | `RESPONSE_BASE_URL`, `RESPONSE_API_TOKEN` | loopback만. 검토는 `ResponseHttpAdapter`, 게시는 `PublicationHttpAdapter` |
| `TOOL_BACKEND=http` | `TOOL_BASE_URL`, `TOOL_API_TOKEN` | loopback만. WRITE는 전송 전에 거절 |
| `RUNTIME_BACKEND=http` | `RUNTIME_BASE_URL`, `RUNTIME_API_TOKEN` | loopback만 |
| `POLICY_BACKEND=http` | `POLICY_BASE_URL`, `POLICY_API_TOKEN` | loopback만 |
| `TRACE_BACKEND=langfuse` | loopback `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_EXPORT_ENABLED=true` | 로컬 trace를 유지하고 allowlist metadata만 OTLP로 export. 비loopback은 `not_implemented` |
| `SCHEDULER_ENABLED=true` | 없음 | `rfa scheduler` 실행 허용 |
| `ENABLE_JUDGE=true`, `JUDGE_PROVIDER=nvidia` | `JUDGE_MODEL`, `NVIDIA_API_KEY` | reserved: `not_implemented` |

`uv run rfa init-env --output .env.dev`는 기존의 비어 있지 않은 값을 덮어쓰지 않고, 통신 구간마다 다른 내부 token과 Langfuse key pair를 생성해 권한 0600으로 저장한다. `NVIDIA_API_KEY`는 외부 발급값이라 생성하지 않는다. 기본 backend는 계속 mock/local이므로 이 명령만으로 외부 호출이 켜지지 않는다.

Raw Slack, GitHub, 메일 credential은 이 서비스에 두지 않는다. key/token은 graph state, prompt, checkpoint, AgentSpec, KB, outbound DTO에 넣지 않는다. 내부 service token은 통신 구간마다 다르며 서로 또는 `NVIDIA_API_KEY`와 재사용하지 않는다. `ALLOW_EXTERNAL_WRITES=true`는 권한을 부여하지 않으며 P0에는 실제 외부 게시 경로가 없다. 모든 provisional HTTP adapter는 loopback URL에만 연결하고 non-loopback URL은 `not_implemented`로 실패한다. 이는 팀원 live API 호환성을 검증했다는 뜻이 아니다.

## 로컬 상태와 trace

| 저장 위치(기본 `DATABASE_URL` 기준) | 내용 |
| --- | --- |
| `.local/rfa.db` | run, DRAFT 버전, 세션, KB source/revision, 효과 ledger, 게시 receipt, 예약, 피드백, event feed. migration 1~13 |
| `.local/rfa.db.checkpoints.sqlite` | LangGraph checkpoint(승인 대기 재개) |
| `.local/rfa.db.scheduler.sqlite` | APScheduler job store(`rfa scheduler` 전용) |
| `TRACE_DIR/rfa-observations-v1/` | 관측 metadata JSONL. DRAFT 본문, 근거 발췌, private 원문, secret은 넣지 않는다 |

장기 KB와 graph checkpoint는 저장 책임이 다르며 팀원 DB나 외부 승인 원본과 직접 공유하지 않는다. LangGraph checkpoint와 DB 사이의 원자성은 가정하지 않는다. 효과가 있는 호출은 효과 ledger에 먼저 기록하고, 결과를 모르는 호출은 조회로만 대사하며 자동으로 다시 실행하지 않는다.

## 한계

- 팀원 실제 Response/Runtime 서비스(P1-008A/B)는 연결하지 않았다. stand-in은 로컬 대체물이다.
- MCP tool 실행과 외부 채널 게시는 없다.
- OpenShell 위의 RFA 역할 실행(P1-007B)과 NemoClaw 운영(P1-007A)은 실행하지 않았다.
- 실제 LLM Judge, Langfuse 보존 기간 적용(P1-006F), 비loopback Langfuse, `nemo_service` backend는 없다.
- 모델 품질과 지연은 n=1이라 분포를 주장하지 않는다.
- production 인증, 멀티테넌시, 분산 queue는 없다. 단일 설치 owner 기준이다.
- 로컬 namespace, `LocalPolicy`, `LocalRuntime`은 OS sandbox나 OpenShell 검증이 아니다. mock 검토 승인은 실제 승인·게시 권한이 아니다.

## 검증 상태

2026-09-27 KST, `wip/stack` 260f394 기준으로 실제 실행한 것:

- 위 "명령" 표의 명령 전부(임시 데이터 디렉터리, 합성 env 파일).
- offline 전체 suite: 아래 "검증 기록" 참고.
- 두 opt-in live smoke(P1-002, P1-003), 각 n=1.

선택 구성으로 container를 만들 수 없을 때의 동작도 확인했다. `MODEL_PROVIDER=nvidia`에 key/model이 없으면 `/healthz`는 200, `/readyz`는 503 `configuration_error`(누락 변수 이름만)이고 작업 route는 503이다. readiness는 설정 preflight이며 provider를 probe하지 않는다.

개발 상태는 [TASKS.md](TASKS.md)의 생성 view, 명세와 검증 evidence는 `tasks/<ID>/task.yaml`에서 조회한다. 작업 운영은 [TASK_EXECUTION_RULES.md](TASK_EXECUTION_RULES.md), 계약과 교체 절차는 [docs/INTEGRATION.md](docs/INTEGRATION.md), 교육 과정 연결은 [docs/EDUCATION_MAPPING.md](docs/EDUCATION_MAPPING.md), 최종 인수 기준은 [E2E 시나리오 10개](RFA_E2E_Test_Scenarios_10_ko.md)다.

### 검증 기록

- 2026-09-27 05:44 KST: `uv run --extra nat --offline --frozen python -m pytest -q -p no:cacheprovider --ignore=tests/test_task_migration.py --ignore=tests/test_taskctl.py` → 1049 passed, 0 failed, 21 skipped(185.4초). skip 21개는 모두 opt-in live test(NVIDIA 5, Retriever 11, Langfuse 4, OpenShell 1)이며 증거가 아니다.
- 같은 시각 `pytest` 실행 파일로 직접 실행하면 `scripts` import 실패로 11개 test 파일이 수집 단계에서 오류가 났다. 그래서 위 명령은 `python -m pytest`를 쓴다.
