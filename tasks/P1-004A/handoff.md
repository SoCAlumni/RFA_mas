# P1-004A — 검토된 지식 축적·provenance

## 구현 사실 (task/P1-004A 16e51d8)

- application/knowledge.py KnowledgeAccumulator: 제안(DerivedItemProposal)은 Supervisor gate를 통과해야만 파생 KB 항목이 된다. 모든 부모를 현재 revision/ACL closure로 다시 읽고, cited는 부모 원문 인용이 확인될 때만(아니면 inferred), 같은 제목의 서로 다른 결정은 conflicting, 합성 실험 수치는 simulated, 검증 전 주장은 tentative. 모든 부모가 public일 때만 public, 아니면 owner.
- extract(): 현재 원문에서 결정/할 일/잠정 주장/링크 줄을 결정적으로 추출. team_proposals(): 완료된 팀 결과의 비교(simulated)와 미검증 수치(tentative).
- Supervisor delegate_team이 완료된 팀 결과를 gate로 축적(worker는 KB에 쓰지 않는다). EpistemicState에 simulated 추가(1.1).

## 검증

- tests/test_knowledge.py 신규 3개(extract 라벨·추론 승격 금지·충돌, stale/restricted 부모 제외·public 재구성, 팀 결과 simulated/tentative 축적) 통과.

## 제한

- 추출은 줄 단위 규칙이며 모델 요약 없음. API 노출은 P1-004B(/v1/knowledge/derive, /derived).

## 재검증 pre-P2-003 (2026-09-26T17:07Z)

- 사유: Direct/transitive dependencies of P2-003 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD d3cad28에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-005 (2026-09-26T17:18Z)

- 사유: Direct/transitive dependencies of P1-005 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 0161cc2에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-021 (2026-09-26T18:07Z)

- 사유: Direct/transitive dependencies of P0-021 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 2223a7f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-008 (2026-09-26T18:23Z)

- 사유: Direct/transitive dependencies of P1-008 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3c5cb6f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-022 (2026-09-26T18:38Z)

- 사유: Direct/transitive dependencies of P0-022 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3466bc5에서 계획된 검증을 worker/target 단계로 재실행한다.
