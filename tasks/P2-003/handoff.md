# P2-003 — 설명 가능한 후보 기본 정렬

## 구현 사실 (84c49f5)

- application/candidates.py rank(): 고정 규칙 candidate-rank-v1(RANK_RULES) — 원문 마감(D-day 구간), 선행 의존성 미충족, 출시/공개 영향, 근거 확실성(cited+/tentative-), 사용자 결정 상태. 각 요소 RankReason 설명, 입력 없는 마감은 0점과 "마감 정보 없음". 안정 tie-break(점수→마감→생성→ID). CandidateService.ranked는 상태를 바꾸지 않는다. RankReason/RankedCandidate 1.1.

## 검증

- tests/test_candidates.py 정렬 테스트(임박 blocker > 기한 없는 가설 아이디어, 이유, 반복 동일, 상태 불변, tie-break).

