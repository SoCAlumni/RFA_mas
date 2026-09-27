# P0-024 — 변경 이벤트·누락 실행·안전한 알림

- 구현(wip/stack 7a968ac, 77114ad)
  - source revision outbox, job-run ledger(UNIQUE fire time + occurrence), 알림 history, migration 11.
  - job_type별 miss 정책: briefing은 최신 허용 자료로 1회 coalesce하고 24h 초과 알림은 hold한다.
  - candidate_scan은 P1-004B CandidateService를 재사용해 새 근거만 알리고 기각 후보는 재알림하지 않는다.
  - 예약은 승인/게시를 하지 않고 owner-only 알림만 만든다.
  - LedgerScheduledEffectHook이 P0-021 ledger에 run key, kind schedule:<job_type>으로 기록한다.
- 발견·수정한 결함
  - DST gap 비교는 UTC로 한다.
  - 같은 owner/domain discovery 동시 실행의 CAS 경쟁은 직렬화와 1회 재시도로 막았다.
- 개발 검증: test_schedules 43 passed. stack 구간 270 passed.
- 한계
  - 예약 실행은 Run binding이 없어 local trace를 내지 않는다(증거는 ID/수만 담은 schedule_runs).
  - P0-025 notification 이벤트 source는 아직 미연결이다.

## 재검증 pre-P0-025 (2026-09-26T21:14Z)

- 사유: Direct/transitive dependencies of P0-025 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 90569fd에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-007 (2026-09-27T00:13Z)

- 사유: Direct/transitive dependencies of P1-007 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 71b2d09에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 final-local-unlock (2026-09-27T01:38Z)

- 사유: NAT reservation is closed; finish remaining source-dependent tasks once, without extra concurrent claim runners or product edits
- 소스 변경 없이 현재 통합 HEAD 93ae0a3에서 계획된 검증을 worker/target 단계로 재실행한다.
