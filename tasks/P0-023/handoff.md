# P0-023 — APScheduler 3.x 영속 job store·단일 runner

- 구현(wip/stack e26213e; pin은 P0-022 구간 f3b5f3c)
  - AsyncIOScheduler + SQLAlchemyJobStore(SQLite).
  - 결정적 manual clock 테스트 adapter.
  - 안정적 job ID. replace_existing은 정의 변경 시에만 쓴다.
  - owner lock(flock)으로 두 번째 runner를 거절한다(프로세스 간 포함, exit 3 scheduler_owner_locked).
  - FastAPI lifespan/worker에서는 시작하지 않는다. `rfa scheduler` CLI만 쓴다.
  - 재시작 때 persisted next_run을 유지한다.
  - 3.x add_job/CronTrigger, max_instances=1, coalesce, job_type별 misfire.
  - job store에는 고정 dispatcher와 schedule_id만 직렬화한다(0600).
- 개발 검증: test_schedules 37 passed(3회 연속). stack 구간 175 passed.
- 한계
  - 실제 OS 절전/복귀와 장시간 soak는 not_run.
  - DST spring-forward 자동 테스트는 없다.
  - API 변경은 job store 동기화까지 약 30초가 걸린다(그 사이 fire는 상태 재검사 후 skip).

