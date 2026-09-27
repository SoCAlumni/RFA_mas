# P0-020B — team 결과·취소 HTTP

- 구현(wip/stack 575626b, f22e4ad, 54e9776)
  - `GET /v1/runs/{id}/team`: 타인/없는 run/팀 없음은 404, 인증 없음은 401.
  - `POST /v1/runs/{id}/cancel`: cancelling 또는 P0-021 durable cancelled를 반환하고, 종료된 run은 409다.
  - extended.json을 재생성했다.
- 개발 검증: test_api(+team_execution, effect_ledger) 95 passed.

## 재검증 freeze-reval (2026-09-27T00:15Z)

- 사유: Full revalidation after shared-file freeze: settings.py/INTEGRATION.md/bootstrap changes by P1-002/P1-003/P1-006C; no source change in these tasks
- 소스 변경 없이 현재 통합 HEAD 71b2d09에서 계획된 검증을 worker/target 단계로 재실행한다.
