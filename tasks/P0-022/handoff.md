# P0-022 — 안전한 예약 DTO·사용자별 일정 관리

- 구현(wip/stack f3b5f3c, b5b18db, 0049594; scheduler_worker 개발)
  - f3b5f3c는 APScheduler==3.11.3·SQLAlchemy==2.0.54 pin이다. CronTrigger 검증에 필요해 이 구간에 포함했다.
  - SchedulerPort와 Schedule/JobRun/Notification DTO, migration 10.
  - job_type allowlist(kb_refresh/candidate_scan/briefing)만 허용하고 인자는 제한한다. shell·callable·prompt는 없다.
  - 시각은 UTC로 저장하고 사용자 timezone을 둔다. 기본 표시는 Asia/Seoul이다.
  - 생성·조회·비활성·취소는 owner 검사를 하고, 실행 시점에 다시 검사한다. `/v1/schedules` API.
  - P1-004 schedule intent를 ScheduleService로 연결했다. 결정적 문구 규칙을 쓰며 세부가 부족하면 schedule_details_required를 반환한다.
  - `contract_baseline.build_baseline`이 새 port를 frozen 1.0 surface에 넣던 결함을 고쳤다(1.0 digest 불변).
- 개발 검증: test_schedules 28 passed(P0-022 상태). stack 구간 195 passed, intent 연결 뒤 134 passed.
- 한계: 예약 ID별 멱등성은 P0-024/P0-021 hook에서 완성된다.

## 재검증 pre-P0-025 (2026-09-26T21:12Z)

- 사유: Direct/transitive dependencies of P0-025 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 90569fd에서 계획된 검증을 worker/target 단계로 재실행한다.
