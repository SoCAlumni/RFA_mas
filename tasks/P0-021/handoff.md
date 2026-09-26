# P0-021 — durable effect ledger·재개 멱등성·결과 대사

- 구현(wip/stack 7f53fc8, 562a177; ledger_worker 개발, coordinator 통합)
  - migration 9는 세 테이블을 추가한다.
    - `effect_ledger`: owner·operation key별 intent/inflight/completed/outcome_unknown, payload fingerprint, result ref, 승인 mirror.
    - `run_effect_barriers`: 취소/권한 회수.
    - `run_lineage`: retry_of.
  - ledger row는 가리키는 기존 기록(게시 receipt, role receipt, team slot·task 생성 key)과 같은 SQLite transaction에 쓴다. 중복 저장소가 없다.
  - 시작 시 intent/inflight는 outcome_unknown으로 바꾼다.
  - 검토 제출은 ledger guard를 거친다. 같은 key는 결과를 재사용하고, 다른 payload는 거절한다. unknown은 권한 원본 조회로만 대사하고 재제출하지 않는다.
  - crash 재개: RUNNING run은 checkpoint에서 계속한다. checkpoint는 끝났지만 DB 결과가 없으면 재실행 없이 checkpoint로 마무리한다(checkpoint와 DB의 원자성은 가정하지 않는다).
  - 취소·권한 회수 barrier는 이후 역할·팀·검토·게시를 차단한다. 명시적 retry는 조회 대사 뒤 retry_of로 연결한 새 run이다.
- 발견·수정한 기존 결함
  - 같은 owner가 게시 key를 재사용하면 다른 run의 receipt가 반환되었다.
  - 그래프 중간 crash 뒤 run이 RUNNING/thread_busy로 영구히 남았다.
  - 재시작 후 resume이 검토를 재제출했다.
- 개발 검증: test_resume + test_effect_ledger 47 passed(os._exit 실제 subprocess crash 포함). stack 구간 인접 289 passed.
- 한계
  - 역할/팀 효과는 local runtime에 프로세스 간 status가 없어 조회 대사가 불가하다. outcome_unknown으로 남고 reconcile은 unsupported를 보고한다.
  - 단일 domain task는 읽기 전용이라 ledger 대상이 아니다.
  - 중복 run 요청은 여전히 409다.
  - effects/reconcile/retry/revoke HTTP 경로는 없다(서비스 메서드만).

## 재검증 pre-P0-022 (2026-09-26T18:40Z)

- 사유: Direct/transitive dependencies of P0-022 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3466bc5에서 계획된 검증을 worker/target 단계로 재실행한다.
