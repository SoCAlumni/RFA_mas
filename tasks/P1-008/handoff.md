# P1-008 — Response/Tool/Runtime/Policy reference 계약 완성

- 구현(wip/stack 58417c5, b121ec7; ledger_worker 개발)
  - `RuntimeHttpAdapter.prepare/cleanup`: timeout은 OutcomeUnknownError로 처리한다.
  - `build_container(http_transport=)`와 `Container.service_owner_id()`, `ui/__init__.py`.
  - `PublicationHttpAdapter`(Publisher protocol): response_backend=http에서만 쓰고 mock fallback이 없다.
    - 게시 POST 전에 stand-in 승인을 확인한다: approved, 미만료, 설치 owner 승인자, draft/version/content/target/policy/sources binding, core payload hash 재계산.
    - timeout·전송 오류·5xx는 outcome_unknown이고 receipt 조회로만 대사한다.
  - 미지원 schema version은 명시적 `unsupported_contract_version` 오류다.
  - 서명 callback 검증기: HMAC raw bytes, timestamp window, 승인자·현재 draft binding, 멱등 replay.
- 개발 검증: test_http_contract + test_callbacks + test_consumer_safety 60 passed(mock·reference HTTP 동일 suite). stack 인접 188 passed.
- 한계
  - `rfa local-stack` CLI는 미구현이다.
  - core graph는 1.0 형식으로 검토를 제출한다. stand-in 승인 참조는 1.1 제출에만 발급되며, 테스트는 operator 단계로 1.1 사본을 승인한다.
  - callback 검증기는 core route에 미장착이고 replay 추적은 in-memory다.
  - HTTP runtime의 팀 실행은 명시적 unsupported다.
  - 모두 local/mock(simulated)이다. 실제 teammate/MCP/OpenShell/게시가 아니다.

## 재검증 pre-P0-025 (2026-09-26T21:13Z)

- 사유: Direct/transitive dependencies of P0-025 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 90569fd에서 계획된 검증을 worker/target 단계로 재실행한다.
