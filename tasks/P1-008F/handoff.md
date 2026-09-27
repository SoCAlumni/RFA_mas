# P1-008F — 팀원 교체 전 로컬 PoC

- 새 독립 조립 모듈 `src/rfa_mas/poc/bootstrap.py`, 실행 `uv run python -m rfa_mas.poc --data-dir .local/poc --port 8780`.
- 기존 core/bootstrap/graph/DTO/settings/lock/UI 소스는 그대로다. native LocalRuntime 사용; OS sandbox 아님. review 원본은 별도 SQLite, UI와 core는 기존 HTTP 계약으로 연결한다. 실제 외부 게시 없음.
- 구형 검토 1.0에는 게시용 ApprovalReference가 없어 PoC 전용 Response mapper가 1.1로 변환한다. ResumePolicy 실제 검사 뒤 정책 참조를 부여하고 수동 승인만 받는다. UI 게시도 core publication API를 거쳐 최신 정책과 durable idempotency를 적용한다.
- loopback 한 포트, 내부 ASGI 서비스, 랜덤 process-only token, ambient/env 파일 비활성, data-dir 단일 프로세스 flock, Ctrl-C/재시작 보존. 실제 팀원 교체는 포트/조립/mapper에서 수행; 기존 real gate는 not_run 유지.
- 개발 cycle1: 4 passed/2 failed, ReferenceHttpClient 생성 시 필수 max_read_retries 누락(TypeError). cycle2: 4 passed/2 failed, response가 EffectGuardedResponse로 감싸져 observer 직접 접근 실패(AttributeError). 생성 인자 지정 및 service.observations 공식 인스턴스 사용으로 수정; service.start가 effect ledger wrapper를 다시 적용함을 코드로 확인했다. 검증을 우회/제거하지 않았다.
- cycle3: tests/test_poc.py 6 passed, 1 warning (기존 내부 subgraph의 durability/no-checkpointer warning). 실제 subprocess 세 번 시작/정지, pending 승인 복구·수동 승인·모의 게시·재게시 방지·receipt 보존, 정책 변경 후 게시 거절, 환경/Origin/CSRF/포트/lock을 확인. ruff check/format, diff check 통과.
- 산출물 사용법/교체 경계: docs/POC.md. 제품 전체 E2E를 반복하지 않는다. worker/target evidence 확인 뒤 main 통합 및 close, 마지막 main 실행 주소를 사용자에게 전달한다.
