# P1-005 — 대상별 DRAFT·읽기/공유/외부 전송 정책

## 구현 사실 (16b8560, P1-001B 위)

- domain graph: owner/public local 대상은 P1-001B staged loader(L0 manifest→L1→예산 내 L2)로 context를 구성하고 통계·insufficient를 ModelRequest와 TaskResult(context)에 남긴다. 그 외 대상/엔드포인트는 ACL-bound retrieval.
- 모델 호출 전 share_egress_filter: 대상에 공유 불가 audience, 비-owner 대상의 비공개 marker(합성 canary + owner 공개 선호 marker hook), cloud endpoint의 비공개 항목을 통째로 보류(문자열 삭제 아님). 공개 대상 요청문 자체에 비공개 marker가 있으면 policy_denied. DRAFT는 통과한 근거만 인용.
- bootstrap: 신뢰된 staged-context factory(retrieval 관측 유지), model endpoint(mock=local, 그 외 cloud).

## 검증

- tests/test_policy.py 신규 6(공개 초안 공유·screen·loader, context 통계/insufficient, 공개 요청문 marker 거절, egress 매트릭스 local/cloud) + graph/trace/resume/api 회귀 88 passed(개발 확인). P1-006E RT03(public 라벨에 섞인 canary의 모델 전달) 결함을 해소하는 경로.

## 제한

- 개인 공개 선호 marker 저장은 P1-005B. BoundAccess는 owner/public local만 지원하므로 company/BU 대상은 retrieval 경로+screen.

## 재검증 pre-P0-021 (2026-09-26T18:08Z)

- 사유: Direct/transitive dependencies of P0-021 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 2223a7f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-008 (2026-09-26T18:24Z)

- 사유: Direct/transitive dependencies of P1-008 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3c5cb6f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-022 (2026-09-26T18:38Z)

- 사유: Direct/transitive dependencies of P0-022 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3466bc5에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-024 (2026-09-26T19:55Z)

- 사유: Direct/transitive dependencies of P0-024 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 6e5494f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-005B (2026-09-26T20:17Z)

- 사유: Direct/transitive dependencies of P1-005B before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 68d474f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-020B (2026-09-26T20:45Z)

- 사유: Direct/transitive dependencies of P0-020B before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 1a1e59f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-025 (2026-09-26T21:09Z)

- 사유: Direct/transitive dependencies of P0-025 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 90569fd에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-005A (2026-09-26T21:33Z)

- 사유: Direct/transitive dependencies of P0-005A before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 71bf2af에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-008E (2026-09-26T21:52Z)

- 사유: Direct/transitive dependencies of P1-008E before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3728423에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-002 (2026-09-26T23:15Z)

- 사유: Direct/transitive dependencies of P1-002 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 88c33d9에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-003 (2026-09-26T23:36Z)

- 사유: Direct/transitive dependencies of P1-003 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 8b06ac8에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-007 (2026-09-27T00:05Z)

- 사유: Direct/transitive dependencies of P1-007 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 71b2d09에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 final-local-unlock (2026-09-27T01:37Z)

- 사유: NAT reservation is closed; finish remaining source-dependent tasks once, without extra concurrent claim runners or product edits
- 소스 변경 없이 현재 통합 HEAD 93ae0a3에서 계획된 검증을 worker/target 단계로 재실행한다.
