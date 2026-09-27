# P1-001B — KB L0/L1/L2 선택적 context loader

## 구현 사실 (wip/P1-001B 212cfb9, 보조 worker knowledge_worker)

- src/rfa_mas/application/context.py ContextLoader.load(): bound identity/role/target/endpoint 확인 → 필수 지침·목표·권한 규칙(untrusted context 규칙 포함) 예산 선점 → L0 metadata manifest(본문 0회)와 정책 receipt → 기존 L1 요약 배치(없으면 none_available) → 남은 문자 예산 내 선택 L2 본문만 읽기. source별 stage와 실측 문자 수 기록, 이전 bundle은 현재 revision/ACL manifest와 hash가 맞을 때만 재사용. ranker hook은 인가 후 metadata만. 쓰기/요약/복제/모델 호출 없음.

## 검증

- tests/test_context_loading.py 17 passed(+관련 회귀 164). 의도적 결함 3종(예산 무시, 요약 coverage 제거, 재사용 hash 검사 해제)이 테스트로 탐지됨.

## 제한

- 전체 source 단위 L2(구간 읽기 없음), 문자 수 예산(token 아님), local endpoint/owner·public target만, graph 연결은 P1-005.

## 재검증 pre-P1-005 (2026-09-26T17:16Z)

- 사유: Direct/transitive dependencies of P1-005 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 0161cc2에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-021 (2026-09-26T18:05Z)

- 사유: Direct/transitive dependencies of P0-021 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 2223a7f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-008 (2026-09-26T18:21Z)

- 사유: Direct/transitive dependencies of P1-008 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3c5cb6f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-022 (2026-09-26T18:36Z)

- 사유: Direct/transitive dependencies of P0-022 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3466bc5에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-024 (2026-09-26T19:54Z)

- 사유: Direct/transitive dependencies of P0-024 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 6e5494f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-005B (2026-09-26T20:14Z)

- 사유: Direct/transitive dependencies of P1-005B before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 68d474f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-020B (2026-09-26T20:43Z)

- 사유: Direct/transitive dependencies of P0-020B before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 1a1e59f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-025 (2026-09-26T21:05Z)

- 사유: Direct/transitive dependencies of P0-025 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 90569fd에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-005A (2026-09-26T21:29Z)

- 사유: Direct/transitive dependencies of P0-005A before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 71bf2af에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-008E (2026-09-26T21:50Z)

- 사유: Direct/transitive dependencies of P1-008E before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3728423에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-002 (2026-09-26T23:12Z)

- 사유: Direct/transitive dependencies of P1-002 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 88c33d9에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-003 (2026-09-26T23:32Z)

- 사유: Direct/transitive dependencies of P1-003 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 8b06ac8에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-007 (2026-09-27T00:01Z)

- 사유: Direct/transitive dependencies of P1-007 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 71b2d09에서 계획된 검증을 worker/target 단계로 재실행한다.
