# P2-003 — 설명 가능한 후보 기본 정렬

## 구현 사실 (84c49f5)

- application/candidates.py rank(): 고정 규칙 candidate-rank-v1(RANK_RULES) — 원문 마감(D-day 구간), 선행 의존성 미충족, 출시/공개 영향, 근거 확실성(cited+/tentative-), 사용자 결정 상태. 각 요소 RankReason 설명, 입력 없는 마감은 0점과 "마감 정보 없음". 안정 tie-break(점수→마감→생성→ID). CandidateService.ranked는 상태를 바꾸지 않는다. RankReason/RankedCandidate 1.1.

## 검증

- tests/test_candidates.py 정렬 테스트(임박 blocker > 기한 없는 가설 아이디어, 이유, 반복 동일, 상태 불변, tie-break).

## 재검증 pre-P0-024 (2026-09-26T19:56Z)

- 사유: Direct/transitive dependencies of P0-024 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 6e5494f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-025 (2026-09-26T21:10Z)

- 사유: Direct/transitive dependencies of P0-025 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 90569fd에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-004C (2026-09-26T22:12Z)

- 사유: Direct/transitive dependencies of P1-004C before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 81b1c08에서 계획된 검증을 worker/target 단계로 재실행한다.
