# LLMOps 증거

이 문서는 LLMOps 작업(평가 회귀, 외부 관측 export)을 실제로 실행한 결과와 아직 검증하지 못한 범위를 구분해 기록한다. mock 성공을 실제 결과로 쓰지 않고, skip은 증거가 아니며, 키 값은 기록하지 않는다.

## P1-006B — Persona 회귀 v2

이 문서는 실제로 실행한 결과만 기록한다. 모든 수치는 합성 fixture, 격리된 임시 SQLite, mock 모델(`mock-model`)로 얻은 **simulated** 결과다. 실제 NVIDIA 모델, 외부 Judge, 제품 최종 gate 결과가 아니다. `semantic_quality`와 `product_final_gate`는 항상 `not_run`이다.

### 데이터셋

- `fixtures/eval/persona_regression_v2.jsonl`: 4개 fixture identity(owner, colleague, other_unit, external) × 6개 상황(근거 있음, 근거 부족, 근거 충돌, 개인정보 혼입, 사칭, 갱신) = 24개. dataset `persona-regression-v2`, seed 29.
- 기존 ID는 그대로 둔다. 각 사례는 `crosswalk_core`(C01~C12)와 `crosswalk_legacy`(기존 24개 ID)로 관련 사례를 가리킨다.
- 입력 문장의 신원/권한 주장은 권한이 아니다. 실행 principal은 항상 `identity_fixture_id`의 고정 fixture identity다.
- 충돌·혼입·갱신 사례는 사례마다 격리된 컨테이너에 합성 노트를 실제 Knowledge API로 기록한다. 팀 노트는 팀 구성원 fixture가 작성한다(설치 owner는 회사 소속이 없어 정책상 거절됨).

### 실행과 비교 규칙

- `rfa evaluate --dataset persona-regression-v2 --label <이름> --output <새 파일>`: 24개 실행 manifest(입력 digest, dataset digest, policy version, code/evaluator/template digest, 모델 adapter, 런타임 버전, 사례별 규칙 관찰·보안 gate·점수·관찰 digest)를 새 파일로만 기록한다.
- `rfa evaluate-compare --baseline A --candidate B`: dataset digest·seed·policy version·실행 모드·사례 집합이 같은 서로 다른 두 실제 실행만 비교한다. 다르면 `comparable=false`와 이유만 남기고 점수를 비교하지 않는다.
- simulated와 actual 집계는 합치지 않는다. `not_run`은 점수가 없다. actual 모드는 `--allow-actual` 명시 opt-in이 필요하며, 실제 provider가 없으면 모든 사례가 `not_run`(`actual_provider_unavailable`)이다. mock 결과를 actual로 표기하지 않는다.
- 보안 gate 실패 또는 규칙 실패가 하나라도 있으면 release gate는 `fail`이다. 비교에서는 candidate의 보안 회귀나 상태 하락도 `fail`이다.
- 관찰 digest는 순서·top-k와 무관한 관찰(규칙 상태, 실행 상태, 기대 근거 인용 여부, 금지 문자열 검출, 모델 호출 수)만 해시한다. 원문 초안·trace·canary는 manifest에 넣지 않는다.

### 측정 결과 (2026-09-26, branch `wip/P1-006B`, base main a0a9347)

환경: CPython 3.12.13, Darwin-arm64, SQLite 3.53.1, langgraph 1.2.12, langchain-core 1.6.5, pydantic 2.13.5, fastapi 0.141.1. code digest `src-8bdcbfd40df752fc`, dataset digest `8fdad15f962d5f6b…`, policy `local-v1`.

| 실행 | 실행 사례 | pass | fail | unknown/error | 보안 실패 | 평균 점수 | release | exit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline (simulated) | 24/24 | 19 | 5 | 0/0 | 4 | 0.9583 | fail | 1 |
| candidate (같은 코드, simulated) | 24/24 | 19 | 5 | 0/0 | 4 | 0.9583 | fail | 1 |

비교: `comparable=true`, 버전/환경 차이 없음, 24개 모두 `unchanged`, 관찰 digest 24/24 일치, 보안 회귀 0, release `fail`(exit 1). 이것은 같은 버전의 결정적 replay 확인이지 개선/악화 주장이 아니다.

실패 사례(현재 main 동작):

- `PR2-{owner,colleague,other-unit,external}-private-mixed`: public 라벨 노트에 섞인 합성 private canary가 public 대상 모델 요청에 도달한다(`model_context`, `forbidden_absent` 실패). main에는 모델 호출 전 내용 screen이 없다. 미통합 P1-005 체인의 share/content screen이 이 경계를 다룬다. 통합 후 같은 dataset/policy로 다시 실행해야 개선을 주장할 수 있다.
- `PR2-colleague-evidence-insufficient`: 무관한 질의가 흔한 토큰(사내)으로 사내 가이드와 lexical match되어 "근거 부족" 대신 무관한 근거를 인용한다(기능 실패, 보안 gate는 pass).

### 테스트로 확인한 회귀 탐지

`tests/test_evaluation.py`는 retrieval이 owner 전용 canary 노트를 모든 요청에 끼워 넣는 의도적 정책 위반을 주입한 candidate를 실행한다. 비교는 `comparable=true`를 유지하면서 비 owner 사례의 `source_scope` pass→fail 보안 회귀와 release `fail`을 보고한다. owner 대상 사례는 오탐하지 않는다.

### 미실행 / 한계

- actual 모델 비교: provider 미구현으로 not_run. Judge/semantic 품질: not_run.
- 근거 충돌의 "잠정/충돌" 명시 표기(`conflict_state`)는 관찰 수단이 없어 요구하지 않았다. `conflict_surfaced`는 두 충돌 source가 현재 revision으로 구조적 인용됐는지만 본다.
- lexical retriever의 동점 순서가 비결정적이라 인용 목록 전체는 digest에서 제외하고 manifest에만 기록한다.

## P1-006C — Langfuse trace export, redaction, 보존·삭제 (2026-09-27 KST)

### 결론

로컬 self-host Langfuse 4.46.0에서 제품 container(TRACE_BACKEND=langfuse)를 한 번 실행했다. 14개 관측을 OTLP로 보냈고, 14개 모두 ID로 다시 조회됐다. 저장된 값에는 canary, query 본문, draft 본문, 원본 run ID, 사용자 ID, 키가 없었다.

연결 실패는 14건 모두 `failed: connection_error`로 기록됐다. 이 경우에도 run과 로컬 trace는 정상이었다. trace ID 기반 삭제(`DELETE /api/public/traces/{id}`)도 실제로 동작했다. 조회에서 사라지기까지 10–51초가 걸렸다.

Langfuse 쪽 보존 기간은 적용되지 않았다. `LANGFUSE_INIT_PROJECT_RETENTION=7`을 설정했지만 Langfuse 4.46은 Enterprise `data-retention` entitlement가 있을 때만 이 값을 적용한다. OSS에서는 값이 조용히 무시되어 project `retention_days`가 NULL로 남는다. 이 공백은 아래 P1-006F의 자체 삭제 job으로 해결했다.

### 구현 요약

| 항목 | 내용 |
| --- | --- |
| 경계 | `LangfuseExportTrace`(`src/rfa_mas/adapters/langfuse.py`)가 기존 `LocalJsonlTrace`를 감싼다. 로컬 JSONL과 SQLite 원장이 원본이다. 로컬 trace가 DB와 대조해 기록한 record만 export한다. export는 부가 기능이라 실패해도 run을 실패시키지 않는다 |
| 전송 | `LangfuseOtlpExporter`: record 하나당 OTLP/HTTP JSON span 하나를 `POST /api/public/otel/v1/traces`로 보낸다. Basic Auth와 `x-langfuse-ingestion-version: 4` header를 쓰고 재시도하지 않는다. `TraceExporter` protocol 뒤에 있어 transport를 교체할 수 있다 |
| 전송 방식 선택 이유 | 공식 compose가 v4 image를 고정한다. v4 기본 `events_only` 모드는 legacy `/api/public/ingestion`의 trace/observation 유형을 거절한다(본 실행에서도 legacy `GET /api/public/traces/{id}`는 404, "not available ... in Langfuse v4 events_only mode") |
| allowlist | 명시한 field 경로만 보낸다. 대상은 observation/sequence/origin/provider 참조, 경계·상태·mode·사유 코드, 호출 수, duration, execution alias, version 참조다. status message와 span event는 비운다. 직렬화된 body에 설정된 secret이 들어 있으면 전송하지 않는다(`not_attempted: redaction_blocked`) |
| token | 제공되지 않은 token은 속성을 보내지 않고 metadata에 `token_usage=not_reported`를 남긴다. 측정된 0은 0으로 보낸다 |
| egress 허가 | `TRACE_BACKEND=langfuse`, loopback `LANGFUSE_BASE_URL`, `LANGFUSE_EXPORT_ENABLED=true`가 모두 필요하다. 키만으로는 허가되지 않는다. 비loopback은 `endpoint:LANGFUSE_BASE_URL:non_loopback`(not implemented)이다. exporter도 매 요청 전에 같은 허가를 다시 검사한다 |
| 결과 기록 | receipt는 `exported` / `failed` / `not_attempted`와 고정 사유 코드만 담는다. 서버 응답 본문은 저장하지 않는다. 확인 응답을 받은 2xx만 `exported`다. `partialSuccess.rejectedSpans>0`, legacy `errors`, JSON이 아닌 2xx 응답은 `failed`다. receipt는 프로세스 메모리에 최대 10,000개까지 보관하며 durable하지 않다 |

### 실행 환경

| 항목 | 값 |
| --- | --- |
| host | macOS 26.5.2, Apple M4, 16 GiB |
| VM | colima 0.10.1, `colima start --cpu 4 --memory 8` (이 작업에서 시작하고 끝에 정지) |
| Docker | Engine 29.2.1, Docker Compose 5.1.1(Homebrew `docker-compose`) |
| Langfuse web | `docker.langfuse.com/langfuse/langfuse:4` @ `sha256:755b821ba8f73a20d43d90e68f2b75d2f597dd164c55890f67a05f5a8d0f0e24`, `/api/public/health` → `{"status":"OK","version":"4.46.0"}` |
| Langfuse worker | `docker.langfuse.com/langfuse/langfuse-worker:4` @ `sha256:3568d2d1eb5dd570f4087b37972ddffa3f6ae4f872553e0b0e2eedf4896a29e9` |
| 저장소 | ClickHouse 25.12, Postgres 17, Redis 7, MinIO(chainguard) |
| compose | 공식 `main/docker-compose.yml` (sha256 `d0309ef3072dba426c6f81f56d46f446361099a8fc0fdf1b042bdf822d85ac3a`). 수정은 네 줄이다. web `3000`, minio `9090`을 `127.0.0.1`에 바인딩했고, host의 기존 Postgres와 겹치지 않도록 postgres host port를 `15432`로 바꿨다. 또 web env에 `LANGFUSE_INIT_PROJECT_RETENTION`을 전달했다 |
| 비밀값 | 모든 `CHANGEME` 값과 project key pair는 실행 시 `openssl rand`로 생성했다. 저장소 밖 임시 디렉터리에 0600 파일로 두었고 출력하지 않았다. `TELEMETRY_ENABLED=false`. 저장소 `.env`/`.env.dev`는 읽지 않았다 |
| client | Python 3.12.13, httpx 0.28.1 |

### 명령

```
colima start --cpu 4 --memory 8
docker-compose -p rfa-lf-p1006c up -d            # 임시 디렉터리, 생성된 .env
RFA_LANGFUSE_LIVE=1 RFA_LANGFUSE_ENV_FILE=<tmp>/langfuse-live.env \
RFA_LANGFUSE_EVIDENCE_OUT=<tmp>/evidence.json \
  .venv/bin/python -m pytest -q -rA tests/integration/test_langfuse_live.py
docker-compose -p rfa-lf-p1006c down -v
colima stop
```

최종 live 실행은 2026-09-26T18:16:36Z–18:17:38Z였고 결과는 **3 passed, 1 failed**다. 실패한 것은 retention이며 아래 blocker를 참고한다.

### 관측 결과

| AC | 검사 | 결과 |
| --- | --- | --- |
| AC1 | canary 요청(`request_id`/`trace_id`/`agent_id`/query에 canary) 1회 export | 관측 14개, receipt 14/14 `exported`, OTel trace 1개(run당 1개), 경계 7종: request/retrieval/policy/model/runtime/approval/stop |
| AC1 | 저장값 검사(v2 observations, fields core,basic,time,metadata,usage,io) | canary, query 본문, draft 본문, 원본 run ID, 사용자 ID, public·secret key 모두 없음. input/output은 비어 있음 |
| AC1 | 저장된 metadata key | allowlist 외에는 Langfuse가 복사한 우리 고정 상수 4개뿐이다: `attributes.langfuse.trace.name`, `resourceAttributes.service.name`, `scope.name`, `scope.version`. 그 밖의 key가 있으면 live test가 실패한다 |
| AC1 | token | `usageDetails={}`, metadata `token_usage=not_reported`. Langfuse가 계산하는 `inputUsage`/`outputUsage`/`totalUsage` 열은 **0으로 표시된다**. 우리가 보낸 값이 아니므로 측정값으로 읽지 않는다 |
| AC2 | 닫힌 loopback port로 export | receipt 14/14 `failed: connection_error`, run COMPLETED, 로컬 trace 유지 |
| AC2 | 첫 live 실행(수정 전) | Langfuse는 14개를 받았다. 그러나 4.46의 200 응답은 queued ingestion job 객체이고, 이를 bare OTLP 응답만 허용하던 adapter가 `failed: invalid_response`로 기록했다. 오판 방향은 보수적이었다(성공을 실패로 기록). 응답 규칙을 수정한 뒤 read-back으로 확인했다. 이 응답 본문은 project public key와 auth scope를 되돌려 주므로 receipt에 본문을 남기지 않는 것이 필요하다 |
| AC3 | ID 기반 조회 | span ID(`id` 필터 + 시간 범위)로 14/14를 3.8초 안에 조회했다. 각 행의 `traceId`와 metadata `observation_id`가 receipt와 일치했다 |
| AC3 | 삭제 | `DELETE /api/public/traces/{otelTraceId}` → 200 `{"message":"Trace deleted successfully"}`. v2 조회에서 사라지기까지 10초(수동), 20.2초, 51.3초(test)가 걸렸다. 한도는 120초다 |
| AC3 | 삭제 범위 | 삭제한 trace의 **원본 OTLP upload blob이 MinIO `events/otel/<project>/…`에 남아 있었다.** 예: 18:12에 올라간 14개 파일은 삭제 후에도 그대로였다. blob 수명 주기는 검증하지 않았다 |
| AC3 | 보존 기간 | **blocked.** web container env `LANGFUSE_INIT_PROJECT_RETENTION=7`이었지만 Postgres `projects.retention_days`는 NULL이고, `GET /api/public/projects`에는 `retentionDays` 필드가 없다(keys: id, metadata, name, organization). 4.46 init 코드는 이 값을 `hasEntitlementBasedOnPlan(..., "data-retention")`일 때만 적용하고, worker의 retention cleaner는 `retentionDays > 0`인 project만 처리한다 |
| AC3 | 실제 만료 | not verified(nightly job) |

로컬 쪽 보존은 이번 작업에서 바꾸지 않았다. `TRACE_RETENTION_DAYS`는 `TRACE_DIR/rfa-observations-v1` 소유 JSONL에만 적용된다.

### 오프라인 검증(외부 서비스 없음)

- `.venv/bin/python -m pytest -q tests/test_security.py tests/integration/test_langfuse_live.py`: 37 passed, 4 skipped. skip된 것은 opt-in live test이며 증거가 아니다.
- 대상: canary/secret 미전송(in-process `httpx.MockTransport` OTLP sink, 제품 container 전체 run), 부분 거절·legacy 207·401·500·비JSON 2xx, 연결/timeout 실패, 허가 없음·비loopback·키 없음이면 요청 0건, null/0/측정 token, 로컬에서 검증되지 않은 record 미export, receipt 불변식.
- 이웃 suite(settings, trace_contract, trace_eval_contract, api, consumer_safety, local_adapters, dev_env, behavior_verifier)는 230 passed, 5 failed다. 실패 5건(`test_generated_schemas_fixtures_and_legacy_compatibility`, behavior_verifier 4건)은 base c1f48d5에서도 같은 방식으로 실패한다. `build_extended()` 출력은 이번 변경 전후에 동일하다. 전체 `tests`는 930 passed, 14 failed, 11 skipped(1차 commit 기준)였다. 14건 모두 base에서도 실패하며, 이 중 9건은 worktree가 등록된 control root가 아니어서 거절되는 `test_task_migration`이다.

### 한계와 후속

- Langfuse 보존 기간은 P1-006F의 `rfa langfuse-retention`과 MinIO `mc rm --older-than` 정리로 적용한다(아래 절).
- export는 관측마다 동기 요청 1회다(timeout ≤5초, batch·retry 없음). Langfuse가 느리면 run 지연이 늘어난다.
- receipt는 메모리에만 있다. 원본은 로컬 JSONL과 SQLite 원장이다.
- 비loopback Langfuse(Cloud 등)는 reserved다. 원격 egress 정책은 별도 작업이다.
- 진단 과정에서 폐기용 stack의 생성된 public key(secret key 아님)가 응답 본문 일부로 한 번 화면에 표시됐다. stack은 `down -v`로 volume까지 삭제되어 그 key는 더 이상 유효하지 않다. 이 문서에는 값을 남기지 않았다.


## P1-006F — community Langfuse 보존 기간을 앱의 자체 삭제 job으로 적용 (2026-09-27 KST)

### 결론

무료 community(OSS) Langfuse만 쓴다는 결정에 따라 Enterprise 보존 기능 대신 앱이 직접 보존 기간을 적용한다. `rfa langfuse-retention`이 기존 `TRACE_RETENTION_DAYS`(기본 7일)보다 오래된 **이 앱의 trace만** ID로 삭제하고, 다시 조회해 사라졌는지 확인한다. 아래 6개 live 통과 기록은 기존 branch `task/P1-006F`의 실제 실행이다. 통합 시 설정을 기존 보존 값으로 합쳤고, 서비스 표식 누락·외부 span 혼합·페이지 스캔 불완전 시 삭제 금지를 보강했다. 이 보강의 검증은 offline 회귀이며 새 live 삭제 실행으로 표시하지 않는다.

- 실제 시계: 방금 export한 trace는 보존 기간 안이라 만료 0건, 삭제 0건이었다.
- 시계를 8일 앞으로 주입(테스트 전용): 이 앱 trace 2개가 만료로 잡혔고 2개 삭제, 2개 모두 조회에서 사라짐(`still_queryable=0`)을 확인했다. 같은 project에 넣은 다른 앱(`service.name=other-app`) trace는 그대로 남았다. dry-run은 같은 2개를 세기만 하고 삭제 요청을 보내지 않았다.
- CLI: 실제 설정 파일로 `rfa --env-file <tmp> langfuse-retention --dry-run`을 실행해 exit 0, `status=completed`, `retention_days=7`을 받았다. 출력에는 개수와 고정 코드만 있고 key나 trace ID는 없다.
- MinIO 원본 upload 파일: trace 삭제 뒤에도 남는 `events/otel/` 원본 파일은 MinIO에 포함된 무료 `mc`로 정리한다. 44개 중 `--older-than 7d`는 0개를 지웠고(보존 기간 안), 70초 뒤 `--older-than 1m`은 44개를 지워 0개가 됐다.

Langfuse 자체의 야간 만료는 community 버전에 없으므로 검증 대상이 아니다. live test는 대신 project가 서버 쪽 보존 값을 보고하지 않는다는 사실(`retentionDays` 없음)을 확인한다.

### 동작

| 항목 | 내용 |
| --- | --- |
| 대상 선택 | `GET /api/public/v2/observations`로 cutoff(`now - TRACE_RETENTION_DAYS`) 이전 관측을 cursor로 페이지 조회한다. metadata `schema=rfa-langfuse-export-v1`과 명시적 service `rfa-mas`가 필요하다. 동일 trace에 외부/표식 누락 관측이 있으면 제외한다. 32자리 hex가 아닌 ID는 URL에 넣지 않는다. 조회 범위는 cutoff 이전 10년으로 제한된다 |
| 만료 판정 | 후보 trace에 cutoff 이후 관측이 하나라도 있으면 보존한다(`kept_recent_traces`). 기간에 걸친 run을 반쯤 지우지 않는다 |
| 삭제·확인 | `DELETE /api/public/traces/{id}` 후 traceId로 다시 조회한다. 180초 안에 사라지지 않으면 `partial: delete_unconfirmed`이며 성공으로 보고하지 않는다 |
| 허가 | exporter와 같다. loopback `LANGFUSE_BASE_URL`, key pair, `LANGFUSE_EXPORT_ENABLED=true`가 모두 있어야 요청을 보낸다. 없으면 요청 0건으로 `not_attempted` |
| 예산 | 페이지 40×500, 삭제 500건/회. 전체 스캔이 예산에 걸리면 삭제 0건으로 `partial: budget_exhausted`; 운영자가 범위를 점검해야 하며 자동 진전은 보장하지 않는다. 완전 스캔 후 삭제 개수만 초과하면 한도까지만 처리한다 |
| 출력 | 개수, cutoff, 고정 사유 코드만 담는다. exit code는 completed 0, partial/failed 1, not_attempted 2 |
| 범위 밖 | 로컬 JSONL trace(`TRACE_RETENTION_DAYS`가 관리), 다른 앱의 trace |

### live 실행

- 환경: 위 P1-006C와 같은 공식 compose(sha256 `d0309ef3…`), Langfuse web/worker 4.46.0, colima 0.10.1. web·minio·postgres host port는 `127.0.0.1`에만 바인딩했다. `CHANGEME` 값과 project key pair는 `openssl rand`로 저장소 밖 0600 파일에 생성했고 출력하지 않았다. `TELEMETRY_ENABLED=false`.
- 명령: `RFA_LANGFUSE_LIVE=1 RFA_LANGFUSE_ENV_FILE=<tmp>/langfuse-live.env .venv/bin/python -m pytest -q tests/integration/test_langfuse_live.py`
- 결과(2026-09-26T22:41–22:46Z, branch `task/P1-006F`): export·조회, ID 삭제, 닫힌 port 실패, community 보존 부재, retention sweep, CLI dry-run 6개 모두 passed. sweep의 삭제 확인까지 107.8초가 걸렸다.
- 페이지 확인: 같은 stack에서 `limit=2`로 조회하자 `meta.cursor`로 3페이지 6행을 중복 없이 받았다. sweeper의 cursor 처리와 같다.
- 오프라인: `tests/test_langfuse_retention.py` 17개(가짜 Langfuse)가 만료·보존·다른 앱 trace 구분, dry-run 삭제 0건, 허가/키 없음 요청 0건, 잘못된 보존 값, 삭제 미확인 partial, 401/5xx/연결 실패, 페이지 예산, 출력의 key·ID 부재를 확인한다.

### 운영

하루 1회 실행한다. 예시 cron(03:17 KST):

```
17 3 * * * cd /path/to/rfa_mas && .venv/bin/rfa --env-file .env langfuse-retention >> .local/langfuse-retention.log 2>&1
27 3 * * * docker exec <project>-minio-1 sh -c 'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc rm --recursive --force --older-than 7d local/langfuse/events/otel/'
```

MinIO 정리는 project 전체의 원본 upload 파일에 적용된다. 이 Langfuse stack이 이 앱 전용이라는 전제다. 원본 파일은 수집 처리에만 쓰이고 조회 데이터는 ClickHouse에 있다.
