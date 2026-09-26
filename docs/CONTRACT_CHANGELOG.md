# 계약 변경 기록

## 2026-09-26 — P1-001 KB 원문·revision 저장 1.1 (통합·발행 대기)

- KnowledgeDocumentV11/KnowledgeAcl/KnowledgeWrite/KnowledgeDelete/KnowledgeRevision 및 export/행별 receipt DTO, owner 관리 CRUD/import API를 additive로 추가한다. 기존 1.0 DTO/route는 보존하고 정확한 원문 공백·provenance를 1.1에서 유지한다.
- SQLite migration4는 원문 이력, 명시 current head와 opaque provider revision 멱등 receipt를 분리한다. 수정·ACL·삭제는 CAS와 새 서버 revision으로 연결하며 재전송은 head를 되돌리지 않는다. 기존 seed/restart와 모호한 legacy의 제한을 유지하고 mutable upsert overwrite를 거절한다.
- 영향 consumer: P1-001A의 ACL-before-content/current 검색 및 과거 결과 무효화, P1-001B context loading, P0-020의 역할별 근거, P1-005 draft/egress. 이 구현이 검색 전체·승인 원본·외부 전송을 구현하지 않는다. 안전한 private DB/sidecar 보호와 API 의미는 INTEGRATION.md를 따른다.
- Pydantic/ports 원본에서 extended만 재생성한다. 검증은 새로운 task evidence에 기록하고 coordinator가 통합/검증 후 발행한다. 작업 이전 증거 또는 mock 성공을 이번 저장 기능의 완료 증거로 재사용하지 않는다.

## 2026-09-26 — P1-006D typed observation 1.1 (통합·발행 대기)

- `ObservationRecord/ObservationCoverage/ObservationLedger`, WorkRepository 관측 메서드, `TracePort.emit_observation` 추가. Pydantic이 원본이며 extended만 재생성한다. frozen 1.0 모델/route는 유지하고 validation error도 기존 detail list shape의 고정 안전한 값으로 반환한다.
- SQLite migration 2는 durable random alias·원자적 순번·불변 관측 원장을 추가하며 migration 1/기존 실행은 보존한다. 관측 참조는 인증·승인 권한이 아니다. 원본 stage ID가 없는 adapter에는 이를 만들어 넣지 않는다.
- legacy arbitrary emit은 더 이상 파일을 쓰지 않는다. 기본 native tracing의 외부 ambient export는 억제한다. typed local export와 보존 범위/복구 제한은 INTEGRATION.md를 따른다. LangSmith는 기존 lock의 0.14.0을 공개 tracing guard용 직접 의존성으로 승격한다.
- 영향 consumer는 P0-028 NAT, P1-006 계열 평가/레드팀 및 후속 team/runtime observer 확장이다. 실제 NVIDIA/NAT/승인 서비스/OpenShell 실행 gate는 이 로컬 관측의 성공과 별개다. 실제 검증은 task evidence에 기록하며 coordinator 통합 후 새 digest를 발행한다.

## 2026-09-26 — P0-016 영속 review-wakeup 계약 (통합 검증·발행 대기)

- `ResumeRequest` 1.1과 `POST /v1/runs/{run_id}/resume` 추가. 기존 1.0 wire는 유지한다. event는 원본 승인 조회를 깨우는 신호이며 approval bool/임의 thread/principal은 불허한다.
- 현재 owner를 먼저 확인하고 session thread를 서버에서 결정한다. 실제 승인/게시 원본은 ResponsePort 담당에 남고 core DB/checkpoint가 새 승인 권한을 갖지 않는다.
- `WorkRepositoryPort.seed_documents_once`는 초기 합성 fixture만 원자적으로 설치한다. 세션별 미완료 Run은 하나이며 동시 실행/새 요청의 thread_busy는 409다. checkpoint 저장은 대화 메타데이터/KB와 별도 파일이다.
- 영향: session/UI/reference consumer, P0-021 복구 ledger, P0-028 NAT service adapter, KB P1-001/P1-001A 현재 source 정책. domain 중간 복구/실제 게시/runtime 격리는 이 계약의 성공 범위가 아니다.
- extended schema는 기존 생성 도구로 파생한다. coordinator가 실제 회귀 후 새 digest를 발행하며, 발행 전 소비자 지원/실제 서비스 합의를 주장하지 않는다.

## 2026-09-26 — P0-015 소유권·세션 API 보강 (coordinator 승인)

- 기존 1.0 WorkRequest/RunResult wire를 변경하지 않고 SessionCreate/SessionMessage/SessionDetail/RunRecord 1.1을 추가했다. session 생성/목록/상세/이어하기와 미완료 run 상태 API를 구현했다. body identity/thread는 불허하고 server principal을 repository 조건까지 전달한다.
- 설치별 random local owner, 기본 membership 없음. 단일 APP_API_KEY 또는 loopback 개발 peer 검사이며 실제 다영 identity 서비스 구현/검증이 아니다. WorkService.get은 principal 필수로 강화하여 기존 내부 호출자 tests/test_graph.py를 함께 수정했다.
- 보안 검토 후 HTTP 생성/이어하기의 run_id는 body 값과 무관하게 서버 발급으로 변경했다. schema field는 유지하지만 클라이언트는 응답 ID로 조회해야 한다. 타인 run ID의 충돌 여부를 통한 존재 노출을 차단한다. 직접 내부 WorkService 호출은 신뢰된 server 경계다. keyless Forwarded/X-Forwarded-*/X-Real-IP 요청은 거절하며 proxy는 API 인증이 필요하다. Runtime/Response 멱등 key도 소유자별로 분리했다.
- WorkRepositoryPort 기존 메서드는 보존하고 소유권 검사 메서드를 추가했다. 레거시 get_result/create_run은 내부 import/저장 호환용으로만 남으며 사용자 API에서 쓰지 않는다. SQLite migration 1은 owner 미상 데이터를 제한 상태로 보존한다. graph resume/checkpointer 구현은 P0-016이다.
- baseline 1.0은 원래 route/schema의 현재 코드 projection을 정확 비교, extended는 전체 API와 신규 메서드를 export한다. 새 기준 digest는 coordinator가 integration 검증 후 publish-contract하며 기존 consumer가 명시 수락해야 한다. 기존 P0-014 근거를 세션 기능의 성공 근거로 바꾸지 않는다.
- 영향 consumer: P0-016/019/020/021, UI/reference adapter, trace/evaluation의 session/run 참조. Task owner registry는 서버 내부 등록만 허용하며 P0-019의 TaskFactory가 연결한다. 실제 teammate API 합의·OpenShell 강제·외부 write는 별도 gate다.

## 2026-09-26 — P0-014 additive 1.1 구현

- 기존 1.0 wire 모델과 HTTP route를 유지한 채 version literal 1.1인 entity/DTO를 추가했다. 상세 필드·소유권·null/migration 경계는 INTEGRATION.md의 1.1 표와 단일 Pydantic 원본을 따른다. 새 endpoint 구현이나 팀원 합의를 주장하지 않는다.
- RuntimePort에 prepare/cleanup, RetrievalPort에 load_context, TracePort에 emit_event를 선언했다. 기존 adapter의 1.0 메서드는 유지되며 새 메서드 구현/지원 확인은 후속 task다. 기존 DB migration은 아직 없고 additive entity 테이블은 P0-015/019/023에서 담당한다.
- 1.0 baseline/fixture는 변경하지 않는다. baseline의 source hash는 최초 구현 provenance이며 호환성 검사에서 현재 wire schema/기존 메서드/OpenAPI를 다시 대조한다. 현재 소스 해시는 새 extended export가 기록한다. 이는 과거 검증을 새 기능의 검증으로 바꾸는 작업이 아니다.
- 정상/정책 거절/미승인/본문 변경/ACL 변경/중복/결과 불명 fixture는 구조 검증용이며 Eval 상태는 not_run이다. 실제 실행 ledger·HTTP callback·trace exporter 보안 검증은 후속 consumer 작업이다.
- 생성: `scripts/contract_baseline.py write-extended`; 검사: `check`, `check-extended`, `tests/test_contracts.py`, `tests/test_trace_eval_contract.py`. 실제 결과는 task evidence에 기록한다. coordinator 통합 후 publish-contract로 digest를 발행하고 각 consumer가 명시 수락한다.

## 2026-09-26 — 기술 고도화 명세 보강(미발행)

- P0-014의 **planned RFA-EXTENDED 1.1**에 실행/정책/근거/승인 연결, stage-aware context, typed trace allowlist, evaluator별 결과/버전 및 7종 fixture 요구를 보강했다. DTO/schema 파일이나 기존 RFA-DTO 1.0 digest는 이번에 변경하지 않았다. publish-contract를 실행하지 않았으며 팀원 합의도 주장하지 않는다.
- 상세 의미·null 조건·원본 소유권·fixture는 [EVALUATION_CONTEXT.md A](EVALUATION_CONTEXT.md#a--repository-local-계약-변경안)에 있다. Pydantic 단일 원본 → schema/OpenAPI/문서 생성 방향은 유지한다. 기존 allowed/code 및 EvalResult 상태 변경은 호환성/migration 검토 후에만 발행한다.
- 영향 consumer: P1-006D, P0-028, P1-006/006A/B/C/E, P1-001A/B/C, P1-004A, P1-005, P1-008/A/B, P0-026. 새 version/digest 수신 전 소비자 draft/not_run 유지. NAT 설치 후보 spike P0-027은 기존 1.0으로 독립 조사 가능하다.
- 제품 source/계약·설정·lock은 미변경이다. 과거 P0-001~013 완료를 다시 구현하도록 열지 않고 추가 AC를 후속 spec_revision에 기록했다.

## 2026-09-26 — 개발 운영 이관 baseline

- 기존 Pydantic `schema_version=1.0`, port, 현재 run 상태를 repository-local provisional baseline으로 export한다. 생성기는 `scripts/contract_baseline.py`, 생성물은 `docs/contracts/baseline.json`, 합성 예제는 `fixtures/contracts/reference_cases.json`이다. 실제 peer가 v1을 지원한다는 의미가 아니다.
- 단일 DTO 원본은 `src/rfa_mas/contracts/`. OpenAPI/JSON schema/문서는 여기서 생성한다. 생성 schema를 수동 수정하여 두 원본을 만들지 않는다.
- 기존 구현에서 Session/Task/TeamSpec/Schedule 영속 entity/API는 아직 없다. 관계·책임 결정은 PRODUCT_REQUIREMENTS에 보존하며 executable schema·새 실패 fixture는 **P0-014**가 제공한다. 기존 계약 export를 확장 계약 완료로 대신하지 않는다.
- 기준 문서/fixture를 검증한 실제 명령·결과는 이관 보고에 기록한다. 아직 없는 route나 명령은 README에 사용 가능하다고 추가하지 않는다.

## 후속 변경 절차

Coordinator가 변경 이유, 영향 provider/consumer task, 호환성, 데이터 migration 순서, schema/fixture 검증 evidence를 남긴다. 공유 계약은 worker 임의 수정 금지. 활성 claim을 멈추고 handoff/recovery 후 새 기준을 전달한다. 관련 task의 spec_revision/context/검증을 stale로 바꾸며 무관한 과거 완료 이력을 전부 다시 열지 않는다.

새 확장 baseline의 path/version/digest를 `taskctl publish-contract`로 등록한 뒤 consumer는 `edit-spec`으로 새 계약과 unresolved 해소를 명시한다. 팀원 실제 스키마 차이는 adapter/mapper에 국한하고 별도 live gate로 검증한다.
# P0-019 — Task/team lifecycle (additive 1.1, repository-local)

- Existing frozen1.0 DTOs/ports remain unchanged. `TeamSpec` adds optional
  `definition_digest` and `execution_budget` for prior provisional1.1 payload compatibility;
  the new Factory requires both, and never edits the approved `TeamTemplate` budget.
- `MemberLifecycle`, `TeamLifecycle` and extended `TeamInstance` states capture prepared,
  not-started, failed, unknown and cleanup outcomes. Core validates the exact persisted
  owner/task/team/domain/template/pin/budget/member/mode response binding again, including
  Pydantic objects changed through `model_copy`. Schema acceptance alone is not authorization.
- Migration3 preserves registry owner/domain, session relations and observation migration2;
  one non-null primary-key Task slot remains occupied even after unknown/failed cleanup.
  Lifecycle events record fixed operation keys and generation/phase CAS, not trace spans.
- Runtime prepare/cleanup are implemented only for local metadata; this is not an OS sandbox.
  Bootstrap injects the configured raw port and trusted support descriptor into TeamFactory;
  existing graph run keeps its observed wrapper. Lifecycle trace collection is explicitly
  `uncollected`, with SQLite lifecycle records as evidence. Reference HTTP lifecycle is
  unsupported, without fallback. Actual teammate compatibility remains P1-008.
- Runtime/consumer tests and regenerated extended schema are required before publication.
  This entry does not preclaim target integration or real runtime validation.
