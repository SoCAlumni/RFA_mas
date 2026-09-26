# P1-004B — TodoCandidate 수락·보류·기각·중복 제거

## 구현 사실 (fe802d7 + 4258af7)

- application/candidates.py CandidateService: P1-004A extractor로 현재 권한 원문에서 todo/issue 후보 발견. 같은 변화·같은 issue 번호는 하나(발견 순서와 무관하게 가장 풍부한 문장+부모 합집합). 원문에 명시된 마감만 due_date. 검증 전 표현은 tentative.
- accept/defer/reject 이력. 기각은 부모 새 revision과 재제안 허용이 있을 때만 proposed로 재노출. closed/완료나 원문 줄 삭제는 superseded. 수락은 결정 기록뿐이며 Task/team 자동 생성 없음.
- migration7 todo_candidates, API: POST /v1/knowledge/derive, GET /v1/knowledge/derived, POST /v1/candidates/discover, GET /v1/candidates, POST /v1/candidates/{id}/decision.

## 검증

- tests/test_candidates.py(중복 제거·무Task, 기각 재노출 규칙·closed superseded, 타 소유자 차단·API, fingerprint) 반복 실행 안정.
- 첫 개발 커밋은 중복 언급 병합이 발견 순서(무작위 source ID)에 의존해 간헐 실패 → 4258af7에서 수정.
- migration7 추가로 tests/test_sessions.py·test_knowledge.py·test_teams.py의 고정 목록([1..6]) 단언이 실패 → 0cee5e4에서
  "빈틈없는 1..N, N>=7" 단언으로 바꿨다(이후 migration 추가 때 이 파일들을 다시 고치지 않음).

## 재검증 pre-P0-024 (2026-09-26T19:55Z)

- 사유: Direct/transitive dependencies of P0-024 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 6e5494f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-025 (2026-09-26T21:09Z)

- 사유: Direct/transitive dependencies of P0-025 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 90569fd에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-004C (2026-09-26T22:12Z)

- 사유: Direct/transitive dependencies of P1-004C before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 81b1c08에서 계획된 검증을 worker/target 단계로 재실행한다.
