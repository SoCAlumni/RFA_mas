# RFA — 짧은 공통 맥락

일반 PC에서 사용자가 직접 운영하는 개인 비서다. 자체 UI + FastAPI/LangGraph를 유지한다. 제품 Task/Team/Run과 개발 task/session/attempt는 별개다.

## 현재 기준

- 기존 P0-001~013은 기반 구현·offline 검증 이력이다. 확장된 제품 P0 전체 완료가 아니다. 영속 LangGraph resume, 사용자별 세션, 실제 Task 팀, 예약은 아직 없다.
- 현재 milestone: D1 계약·세션·KB → D2 팀·후보·DRAFT → D3 복구·예약·reference·평가 → D4 실제 통합. 기존 D1~D3 코어 24h + 기술 고도화 4.5h, D4 조건부 8h. 증가분은 독립 세션에 배정할 계획이며 직렬 28.5h를 24h 완료로 약속하지 않는다. 용량 부족 시 필수 gate 미완료를 보고한다.
- 작업 명세/상태 원본은 canonical control root의 `tasks/<ID>/task.yaml`이다. `TASKS.md`, index/state는 파생 view다. 제품 기능/실행 명령의 현재 사실은 README·소스·검증 근거로 확인한다.
- Git은 초기화됐지만 HEAD 커밋이 없고 프로젝트 파일이 untracked다(2026-09-26 재확인). 승인된 integration baseline을 등록하지 않았다. OPS-000 해소 전 planning ready와 executable ready를 구분하며 실제 worktree claim은 거절한다. 임의 commit/stash하지 않는다.

## 불변 아키텍처

- `API → application/graph → port`; adapter 주입은 `bootstrap.py`. graph에 URL/auth/MCP SDK를 넣지 않는다. 팀원 DB/checkpoint를 공유하지 않는다.
- LangGraph 영속 checkpointer + 서비스 세션 메타데이터. 별도 KB에 원문·provenance·source revision·사용자 선호. checkpoint는 검색 KB가 아니다.
- APScheduler 3.11.3/SQLAlchemy 영속 job store를 후속 구현에서 lock. 전용 단일 runner; UI 기본 Asia/Seoul, 내부 UTC. 자체 cron engine 금지. PC 절전 중 실행을 보장하지 않는다.
- Domain은 지식 맥락, Task는 지속 업무, Task당 활성 TeamInstance 하나, 후속 Run은 팀 재사용. Session↔Task는 N:M. Benchmark=Supervisor/논문 조사/실험/분석, Research=Supervisor/자료 조사/근거 검토. 미구현 Engineering 선택 금지.
- TeamSelector는 capability·권한·runtime·예산으로 후보를 먼저 제한한다. 역할별 자료/tool/memory 및 팀 총예산, 중복 provisioning·부분 실패·취소를 검증한다.
- 외부 요청·팀 간·팀 내 위임/결과는 Supervisor 경유. P0 Debate 금지; 후속 제한된 DebateLease 밖에서 worker 직접 통신 금지.
- 읽기/공유/cloud egress는 각각 판단한다. audience 순위만으로 승인하지 않고 검증된 membership을 결합한다. 미분류/private 1:1·일정·내부 자원은 자동 공유 금지. 공개 근거를 먼저 제한한 뒤 DRAFT 생성.
- 승인 원본과 draft version/hash/target을 정확히 결합한다. 내용/첨부/대상/ACL/policy 변경은 재검토. timeout의 outcome_unknown을 자동 실패·재게시로 바꾸지 않는다.
- P0는 key/GPU/Docker/외부 서비스 없이 local 로직 + mock 공급자로 완주. 저장·권한·상태·팀 중복·스케줄·승인 검증은 실제 local 코드여야 한다. 외부 write는 flag와 무관하게 금지. local runtime은 sandbox가 아니다.
- NVIDIA 모델, 공식 Skill 도구 연결, NemoClaw 지원 경로, OpenShell 허용/차단은 각각 real 증거. OpenClaw/Deep Agents/Gateway 필수 도입·내부소스 복사 금지. 일반 PC에 전체 RAG/GPU 배포를 요구하지 않는다.
- LLMOps는 민섭: trace/provenance, Judge, Persona QA, 회귀. 결정적 권한/기밀 gate를 Judge 점수로 상쇄하지 않는다. core는 인터페이스와 합성 fixture부터 진행한다.
- NAT는 기존 LangGraph 밖의 선택 adapter다. 호환성 spike 후 대표 비대화형 1경로만 검증하며 checkpoint/인증/실행 원본을 바꾸지 않는다. 기본 실행은 NAT 없이 가능; 설치된 NAT+fake provider 증거와 NVIDIA 모델 실호출은 별개다.
- P0 평가에는 핵심 Persona 12사례·행동 증거 verifier·고정 공격/정상 대조를 둔다. Judge 미실행은 품질 미검증. KB는 기존 RetrievalPort의 L0/L1/L2 선택 로딩과 모든 부모의 최신 ACL/share/egress를 사용한다. OpenViking 전체 엔진·새 관측 서버는 필수가 아니다.
- `.env`/credential 값은 읽기·복사·hash·로그 금지. `.env.example` 비밀 값은 빈칸, doctor는 configured/missing만. 기능 flag는 권한이 아니다.

## 제품 책임과 개발 workstream

민섭: 코어·KB·Task 팀·DRAFT·세션·예약·LLMOps. 승희: 채널·MCP·승인 원본·게시·receipt(Response/Tool). 다영: OpenShell runtime·identity·권한 강제·UI(Runtime). 기밀 검수 최종 소유권은 미확정이며 PolicyPort로 교체한다. mirror/mock은 상대 원본의 권한이 없다.

개발 workstream은 contracts/control, knowledge, agents, execution, integration/evaluation이다. 이는 팀원의 서비스 소유권을 바꾸지 않는다. 공통 DTO/ports/migration 등록부/설정·lock/bootstrap/root conftest는 coordinator가 직렬 관리한다. 기본 worker 2, 독립성이 확인되면 최대 3. worktree 분리만으로 파일/자원 충돌이 해결되지는 않는다.

## 선택한 task만 깊게 읽기

적용 지침 → 이 문서 → [실행 규칙](../TASK_EXECUTION_RULES.md) → 필터된 상태 → 선택 task/context-pack → handoff → 직접 선행 결과·계약 → 필요한 소스 순서다. 전체 task 상세나 과거 로그를 매번 로드하지 않는다.

- [제품 요구사항·일정·최종 gate](PRODUCT_REQUIREMENTS.md): 이관 전 요구사항의 보존본과 우선순위.
- [현재/목표 계약](INTEGRATION.md), [계약 변경 이력](CONTRACT_CHANGELOG.md): Pydantic이 원본, OpenAPI/schema는 파생. 팀원 HTTP는 provisional.
- [추적·NAT·평가·KB 명세](EVALUATION_CONTEXT.md): 해당 task의 절만 읽는다. OpenShell middleware/gateway·승희 서비스·다영 UI 구현은 민섭 범위 밖이며 Policy 소유권은 미확정이다.
- [이관 기록](TASK_MIGRATION.md), [교육/기술 증거](EDUCATION_MAPPING.md).
- [Coordinator](../.agent/prompts/coordinator.md), [Worker](../.agent/prompts/worker.md), [Resume](../.agent/prompts/resume.md).
