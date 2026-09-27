# P0-025 — UI polling 상태·안전한 이벤트·readiness·registry

- 구현(wip/stack a6d39b1, 1e9759d; quality_worker 개발)
  - migration 13 `run_event_feed`(owner별 빈틈없는 sequence = cursor), application/events.py.
  - `GET /v1/runs/{id}/status`, `GET /v1/tasks/{id}/status`, `GET /v1/events?cursor=&limit=&run_id=`.
  - 이벤트는 참조만 담는다. 읽을 때 source를 재승인하고 삭제된 source는 withheld 수로만 센다. trace ID는 권한이 아니다.
  - `/readyz`는 선택 backend별 checks를 보고한다(도달 불가 HTTP, NVIDIA 키 누락은 이름만, 미구현 표시). `/healthz`와 분리했다.
  - `registry/service.json`에는 실제 제공 capability만 넣었다. SSE/notification은 없다.
- 개발 검증: test_api 15 passed(stack 병합 후 18). stack tip 전체 947 passed / 0 failed / 11 skipped(opt-in live).
- 한계: notification 이벤트 kind는 예약만 있고 producer가 없다(P0-024 연결은 후속).

## 재검증 pre-P1-007 (2026-09-27T00:14Z)

- 사유: Direct/transitive dependencies of P1-007 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 71b2d09에서 계획된 검증을 worker/target 단계로 재실행한다.
