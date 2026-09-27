# P1-001D — 한국어 조사 정규화·결정적 BM25(권한 필터 뒤)

- 구현(wip/stack bb23a01, 20cda8f; quality_worker 개발)
  - 질의 term에서 한국어 조사와 요청어를 제거하고, 한글 2자 bigram을 추가한다(2자 이하 토큰은 전체 일치만).
  - BM25는 권한 통과 source에서만 계산한다. SQLite는 term 수와 길이만 반환한다.
  - 동률은 provider/namespace/external_id로 정렬한다. 질의 term은 128개로 제한한다.
- 측정(wip/P0-026 harness a4a57c6, seed 20261001, golden set·목표 0.9 불변)

  | 지표 | 이전 | 이후 |
  |---|---|---|
  | Recall@5 small20 | 0.6087 | 1.0(3회) |
  | Recall@5 load1000 | 0.5217 | 1.0 |
  | 1000문서 검색 median | 43.4ms | 49.4ms |

- 개발 검증: test_retrieval 35 passed. stack 구간 254 passed.

## 재검증 pre-P1-001E (2026-09-26T22:19Z)

- 사유: Direct/transitive dependencies of P1-001E before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD bfd2566에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 completion-prerequisites (2026-09-27T01:03Z)

- 사유: Remaining Judge/bootstrap-dependent prerequisites for retention and the observed Persona relevance fix; single bounded pass
- 소스 변경 없이 현재 통합 HEAD d7d5bbb에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 completion-final-slices (2026-09-27T02:07Z)

- 사유: Final source baseline191378e; UI contract acceptance and identifier relevance changed since old evidence. No product edits.
- 소스 변경 없이 현재 통합 HEAD 191378e에서 계획된 검증을 worker/target 단계로 재실행한다.
