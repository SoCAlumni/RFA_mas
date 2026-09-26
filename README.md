# RFA MAS

NVIDIA Korea Agentic AI Hackathon을 위한 기업형 개인 비서의 P0 기반 구현이다. SRNote의 노트 저장·탐색·지식 축적 개념을 참고하되, 이 저장소는 비서 Supervisor, 도메인 TaskGraph, 근거 제한 DRAFT, 로컬 정책, 교체 가능한 port/adapter 경계에 집중한다.

현재 기본 흐름은 다음과 같다.

```text
합성 질의
  -> 비서 Supervisor (도메인 routing)
  -> 공통 Domain TaskGraph (정책 검사 -> mock 검색 -> mock 생성)
  -> 대상 audience에 맞춘 DRAFT
  -> mock 검토 결과
```

지원하는 초기 도메인은 `triv3`와 `quantization_research`다. 기본 실행은 모델 API, 외부 서비스, GPU, Docker, API key 없이 동작하도록 설계되었다. 모든 mock 결과는 `simulated=true`와 adapter 이름을 포함한다.

## 빠른 시작

필수 환경은 Python 3.12와 [uv](https://docs.astral.sh/uv/)다. 패키지 버전은 `uv.lock`에 고정되어 있다.

```bash
uv sync --locked
uv run rfa doctor
uv run rfa demo
```

기본 mock/local 실행에는 `.env`가 필요 없다. 모든 교체 서비스의 로컬 개발 구성을 미리 준비하려면 아래 명령을 사용한다.

```bash
uv run rfa init-env --output .env.dev
uv run rfa --env-file .env.dev doctor
uv run rfa --env-file .env.dev demo
```

`init-env`는 기존의 비어 있지 않은 값을 덮어쓰지 않고, 서비스마다 다른 내부 token과 Langfuse project key pair를 비추적 env profile에 생성한 뒤 권한을 `0600`으로 제한한다. `.env.dev`는 SQLite, retriever index, trace를 `.local/dev/` 아래에 격리한다. 외부 발급값인 `NVIDIA_API_KEY`는 생성하지 않는다. 출력에는 변수명과 생성/보존 상태만 포함된다. 기본 backend는 계속 `mock`/`local`이므로 이 명령만으로 외부 호출이나 실제 서비스 연동이 활성화되지는 않는다.

`doctor`도 변수명과 `configured`/`missing` 상태만 출력하며 값, 일부 문자열, 길이를 출력하지 않는다.

## 개발 명령

```bash
# 설정 점검
uv run rfa doctor

# 기본 TRIV3 공개 DRAFT demo
uv run rfa demo

# Quantization Research demo
uv run rfa demo --domain quantization_research --audience public

# 재현 가능한 실패/검토 시나리오
uv run rfa demo --scenario insufficient_evidence
uv run rfa demo --scenario policy_denied
uv run rfa demo --scenario revision_requested
uv run rfa demo --scenario timeout

# API
uv run rfa api

# OpenAPI 파일 생성
uv run rfa openapi --output .local/openapi.json

# 품질 검사
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

`policy_denied`와 `timeout` demo는 의도한 실패 상태를 반환하므로 CLI 종료 코드도 성공이 아닐 수 있다. 이것은 실제 외부 장애나 OpenShell 차단 증거가 아니라 결정적 P0 시나리오다.

API를 실행하면 기본 주소는 `http://127.0.0.1:8000`이다.

- `GET /healthz`: 프로세스 health
- `GET /readyz`: SQLite와 fixture 초기화가 끝났는지 확인
- `POST /v1/work`: 작업 실행 및 결과 반환
- `GET /v1/work/{run_id}`: 저장된 결과 조회
- `GET /docs`: Swagger UI
- `GET /openapi.json`: 생성된 OpenAPI

예제 요청:

```bash
# `rfa init-env`로 APP_API_KEY를 생성한 경우, secret이 현재 shell 밖에 남지 않도록
# subshell 안에서만 선택한 profile을 읽는다.
(
  set -a
  source .env.dev
  set +a
  curl -sS http://127.0.0.1:8000/v1/work \
    -H "Authorization: Bearer ${APP_API_KEY}" \
    -H 'Content-Type: application/json' \
    -d '{
      "query": "TRIV3 공개 트랙을 근거와 함께 요약해 줘.",
      "domain_id": "triv3",
      "target": {"audience": "public"}
    }'
)
```

P0의 `POST /v1/work`는 background queue가 아니라 같은 요청 안에서 graph를 실행한다. `GET` endpoint는 완료된 로컬 결과를 조회한다.

## 설정과 key 관리

`.env.example`가 설정 이름의 기준이며 `src/rfa_mas/settings.py`와 1:1로 대응한다. 실제 `.env`, 환경별 secret 파일, `.local/`, DB, 로그, 가상환경은 `.gitignore` 대상이다.

CLI에서 named profile을 사용할 때는 global option을 subcommand 앞에 둔다: `uv run rfa --env-file .env.dev api`. 명시한 파일이 없거나 symlink/비정규 파일이면 조용히 기본값으로 fallback하지 않고 거절한다.

로컬 개발 topology는 서비스 구현 주체와 무관하게 다음 endpoint를 사용한다. `8001`은 여러 계약을 한 프로세스에서 검증하는 기존 reference fixture 전용이고, 실제 교체 서비스는 장애·로그·수명주기를 분리하기 위해 별도 port를 쓴다.

| Port | 서비스 | 설정 |
| ---: | --- | --- |
| 8000 | RFA Supervisor API | `APP_HOST`, `APP_PORT`, `APP_API_KEY` |
| 8001 | 통합 reference fixture | 테스트 전용 |
| 8011 | Response/Review | `RESPONSE_BASE_URL`, `RESPONSE_API_TOKEN` |
| 8012 | MCP/Tool gateway | `TOOL_BASE_URL`, `TOOL_API_TOKEN` |
| 8013 | Agent Runtime | `RUNTIME_BASE_URL`, `RUNTIME_API_TOKEN` |
| 8014 | Policy decision | `POLICY_BASE_URL`, `POLICY_API_TOKEN` |
| 7670 | NeMo Retriever service boundary | `RETRIEVER_SERVICE_URL`, `NEMO_RETRIEVER_API_TOKEN` |
| 3000 | Self-hosted Langfuse | `LANGFUSE_BASE_URL`, Langfuse key pair |

| 선택 시점 | 직접 입력할 변수 | 현재 동작 |
| --- | --- | --- |
| 기본 loopback mock/local | 없음 | P0 기본값 |
| `APP_HOST`를 loopback 밖으로 변경 | `APP_API_KEY` | 작업 API에 bearer 인증 필요. 로컬 identity는 실사용 인증이 아님 |
| `MODEL_PROVIDER=nvidia` | `NVIDIA_MODEL`, `NVIDIA_API_KEY` | reserved; 값이 있어도 P0에서 `not_implemented` |
| `RETRIEVER_BACKEND=nemo_service` | `RETRIEVER_SERVICE_URL`, `NEMO_RETRIEVER_API_TOKEN` | reserved; P1 대상 |
| `RESPONSE_BACKEND=http` | `RESPONSE_BASE_URL`, `RESPONSE_API_TOKEN` | P0에서는 loopback reference fixture만 허용 |
| `TOOL_BACKEND=http` | `TOOL_BASE_URL`, `TOOL_API_TOKEN` | P0에서는 loopback fixture만 허용하며 write는 network 호출 전에 거절 |
| `RUNTIME_BACKEND=http` | `RUNTIME_BASE_URL`, `RUNTIME_API_TOKEN` | P0에서는 loopback reference fixture만 허용 |
| `POLICY_BACKEND=http` | `POLICY_BASE_URL`, `POLICY_API_TOKEN` | P0에서는 loopback reference fixture만 허용 |
| `TRACE_BACKEND=langfuse` | `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | reserved; P0에서 `not_implemented` |
| `ENABLE_JUDGE=true`, `JUDGE_PROVIDER=nvidia` | `JUDGE_MODEL`, `NVIDIA_API_KEY` | reserved; 실제 Judge는 opt-in P1 대상 |

현재 후보는 main model [`nvidia/nemotron-3.5-lightning-30b-a3b`](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b), Judge [`nvidia/nemotron-3-ultra-550b-a55b`](https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b)다. Lightning은 한국어를 공식 지원 언어로 명시하지 않으므로 한국어 golden evaluation에서 기준을 충족하지 못하면 main model도 Ultra로 교체한다. tool calling과 JSON mode는 P1 live smoke test 및 Pydantic validation으로 확인해야 하며, 값이 있다는 것만으로 실제 호출을 주장하지 않는다. `.env.example`은 NVIDIA/OpenShell의 공식 환경변수 규격이 아니라 이 애플리케이션의 설정 계약이다.

Raw Slack, GitHub, 메일 credential은 이 서비스에 두지 않는다. 해당 credential과 게시 실행은 우리가 구현하거나 팀원 구현으로 교체할 tool/response 서비스 또는 runtime credential-delivery 경계에서 관리한다. key/token은 graph state, prompt, checkpoint, AgentSpec, KB, outbound DTO에 넣지 않는다. 내부 service token은 각각 다른 통신 구간의 값이며 서로 또는 `NVIDIA_API_KEY`와 재사용하지 않는다.

`ALLOW_EXTERNAL_WRITES=true`는 권한을 부여하지 않는다. P0의 `MockTool`은 write를 거절하며 실제 외부 게시 경로는 구현되어 있지 않다.
모든 provisional HTTP adapter는 P0에서 loopback URL에만 연결된다. non-loopback URL을 선택하면 `not_implemented`로 실패하며, 이는 팀원 live API 호환성을 검증했다는 의미가 아니다.

## 구조

```text
src/rfa_mas/
├── api/                 # FastAPI와 HTTP 오류 변환
├── application/graphs/  # Supervisor와 공통 Domain TaskGraph
├── contracts/           # Pydantic DTO; OpenAPI의 기준
├── ports/               # async Protocol 경계
├── adapters/            # mock/local/reference HTTP 구현
├── reference/           # 팀원 계약이 아닌 로컬 HTTP fixture
├── bootstrap.py         # composition root와 dependency injection
├── settings.py          # typed settings와 선택 모드 검증
└── security.py          # secret redaction
fixtures/
├── documents/           # 두 도메인의 합성 public/internal/private 자료
└── eval/                # 4 persona x 6 상황의 24개 합성 평가 사례
```

호출 방향은 `API -> application/graph -> port`다. mock/real 선택은 `bootstrap.py`에서만 수행하며 graph node는 팀원의 URL, MCP SDK, 인증 header, OpenShell CLI를 알지 못한다. 상세 계약과 교체 절차는 [docs/INTEGRATION.md](docs/INTEGRATION.md), 교육 자료 연결은 [docs/EDUCATION_MAPPING.md](docs/EDUCATION_MAPPING.md), 개발 순서는 [TASKS.md](TASKS.md)를 참고한다.

## 로컬 상태와 trace

기본 SQLite는 `.local/rfa.db`에 다음 책임을 분리해 저장한다.

- `runs`, `drafts`: 이 서비스의 작업 상태와 DRAFT 버전
- `kb_documents`: 출처·revision·audience가 있는 합성 KB fixture
- `graph_checkpoints`: P0의 최종 graph metadata

P0 `graph_checkpoints`는 LangGraph의 production durable resume/checkpointer를 구현한 것이 아니다. 장기 KB와 checkpoint는 용도가 다르며, 팀원 DB나 외부 승인 원본을 직접 공유하지 않는다.

기본 trace는 `.local/traces/events.jsonl`에 request/trace/run ID, 상태, adapter 같은 metadata를 기록한다. DRAFT 본문, evidence excerpt, private 원문은 trace event에 넣지 않으며 configured secret은 기록 직전에 마스킹한다.

## P0 범위와 한계

현재 구현은 다음을 제공한다.

- Pydantic DTO와 FastAPI OpenAPI
- LangGraph Supervisor와 두 도메인이 공유하는 TaskGraph template
- 합성 fixture, deterministic mock model/retrieval/response/tool/judge
- SQLite 작업·DRAFT·fixture 저장, 로컬 JSONL metadata trace
- membership와 audience를 결합한 로컬 application policy
- process-local `LocalRuntime`
- Response/Tool/Runtime/Policy용 loopback 전용 provisional HTTP adapter와 로컬 contract fixture
- `success`, 근거 부족, 권한 거절, 수정 요청, timeout 시나리오

다음은 아직 제공하지 않는다.

- 실제 NVIDIA 모델 호출 또는 검증된 모델 ID
- NeMo Retriever skill worker, CLI/service index 또는 실제 RAG
- 실제 MCP tool 실행이나 외부 채널 게시
- NemoClaw 등록·지속 운영 또는 OpenShell filesystem/network/process 강제
- production 인증, 멀티테넌시, 분산 queue, durable LangGraph resume
- 노트 입력·수정·장기 기억과 실제 피드백 학습
- 실제 LLM Judge 또는 Langfuse

따라서 로컬 namespace, `LocalPolicy`, `LocalRuntime`을 OS sandbox나 OpenShell 검증으로 설명해서는 안 된다. mock 검토 승인도 승희 서비스의 실제 승인·게시 권한이 아니다. 기밀 검수 Agent의 최종 서비스 소유권 역시 아직 미확정이다.

## 검증 상태

2026-09-25 KST에 key 없는 기본 환경에서 다음을 실제 실행했다.

- `uv sync --locked`: lockfile 기반 환경 확인 성공
- `uv run ruff check .`, `uv run ruff format --check .`: 통과
- `uv run pytest -q`: 전체 offline suite 통과
- `uv run rfa doctor`: `ready=true`, missing/reserved 없음, external write effective `false`
- 두 도메인의 `uv run rfa demo`: `completed`, `simulated=true`, 게시 `not_requested`
- 로컬 API 기동 후 health/readiness/OpenAPI/work create/get 확인
- `.local/verification/openapi.json` export와 schema/path 검사

개발 상태는 [TASKS.md](TASKS.md)의 생성 view, 명세·실제 검증 evidence는 `tasks/<ID>/task.yaml`에서 조회한다. 작업 운영은 [TASK_EXECUTION_RULES.md](TASK_EXECUTION_RULES.md), 이번 이관 검증은 [docs/TASK_MIGRATION.md](docs/TASK_MIGRATION.md)를 참고한다. 실제 NVIDIA 모델, NeMo Retriever, MCP, 팀원 live 서비스, NemoClaw/OpenShell 검증은 수행하지 않았으며 P1의 opt-in 통합 테스트로 남아 있다. 현재 디렉터리는 Git worktree가 아니므로 `.env.example`의 실제 tracked 상태는 확인할 수 없고, 파일 존재와 `.gitignore` 예외만 검증했다.
