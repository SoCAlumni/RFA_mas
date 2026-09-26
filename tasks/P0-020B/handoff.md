# P0-020B — team 결과·취소 HTTP

- 구현(wip/stack 575626b, f22e4ad, 54e9776)
  - `GET /v1/runs/{id}/team`: 타인/없는 run/팀 없음은 404, 인증 없음은 401.
  - `POST /v1/runs/{id}/cancel`: cancelling 또는 P0-021 durable cancelled를 반환하고, 종료된 run은 409다.
  - extended.json을 재생성했다.
- 개발 검증: test_api(+team_execution, effect_ledger) 95 passed.

