# P1-008E — 승인 전 게시가 run을 영구 실패로 만들지 않음

## 원인

- HTTP publisher는 durable PENDING receipt를 쓴 뒤에 stand-in 승인을 확인했다.
- 그래서 거절이 terminal FAILED로 남았고, 이후 승인된 게시도 영구히 막혔다.

## 수정 (wip/P1-008E ac6f84f, ledger_worker)

- Publisher protocol에 `authorize(binding)`를 추가해 아무것도 쓰기 전에 승인을 확인한다.
  - HTTP 경로: 승인이 없거나 대기·만료·다른 승인자·다른 내용이면 approval_required(409)이고 receipt를 남기지 않는다.
  - mock 경로: no-op.
- 게시 POST 직전에도 다시 확인한다. 이때 거절되면 아무것도 전송하지 않았으므로, 손대지 않은 PENDING receipt와 ledger 항목을 한 transaction으로 회수한다(`withdraw_unsent_publication`).
- 이미 진행된 게시는 outcome_unknown으로 유지한다. 실제 전송 뒤 확정 거절은 FAILED다. timeout·5xx는 조회로만 확인하고 재전송하지 않는다.

## 검증 (개발)

- tests/test_consumer_safety.py 32/32 passed. 수정 전 코드에서는 9개 실패.
- 인접 테스트 195 passed. 전체 1053 passed.

## 영향

- E2E-08 harness가 기대하던 조기 게시 결과가 `status == failed`에서 409 approval_required로 바뀐다.

## 재검증 final-local-unlock (2026-09-27T01:43Z)

- 사유: NAT reservation is closed; finish remaining source-dependent tasks once, without extra concurrent claim runners or product edits
- 소스 변경 없이 현재 통합 HEAD 93ae0a3에서 계획된 검증을 worker/target 단계로 재실행한다.
