# 계약 변경 기록

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
