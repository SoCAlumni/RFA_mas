# RFA 제품 요구사항·일정·검증 gate

2026-09-26 이관 시 기존 TASKS.md의 승인된 공통 요구사항을 보존했다. 상태 원본은 각 task.yaml이며 이 문서는 제품 명세다. 아래 공식 자료 확인 기록은 당시 확인 범위이며 새 실행 증거가 아니다. 전체 원문/카드/AC/주석/링크와 V-001~012는 [이관 전 원문](archive/TASKS_2026-09-26.md)에 그대로 있다.

## 확정 결정과 핵심 데모

일반 PC에서 사용자가 운영하는 개인 비서다. 자체 UI와 FastAPI/LangGraph 코어를 유지하며 UI 구현은 다영 모듈과 연결한다. 세션·cron을 위해 OpenClaw Gateway를 필수 의존성으로 추가하지 않는다. OpenClaw 내부 소스 복사·비공개 import, 필수 Deep Agents 도입, 기능별 신규 MSA는 범위 밖이다. 후속 도입은 worker 또는 ChannelAdapter 교체로 한정한다.

| 경계 | 이번 계획의 결정 |
| --- | --- |
| 세션과 KB | LangGraph 영속 SQLite checkpointer + 서비스 세션 메타데이터. 원문/출처/버전/선호는 별도 KB 모델. checkpoint를 검색 index로 사용하지 않음 |
| 식별자 | `conversation_id`는 `session_id`의 외부 별칭이며 별도 entity 아님. Session 1개는 서버 발급 supervisor `thread_id` 1개와 여러 Run을 가짐. Session↔Task는 연결 테이블로 N:M. Task는 domain 1개, 활성 TeamInstance 최대 1개, 여러 Run을 가짐. 단순 저장/질의 run의 task/team은 null 가능. worker checkpoint namespace는 task/team/run/agent로 분리 |
| 재개와 동시 실행 | 같은 run 재개는 같은 thread/namespace 사용. 같은 thread의 동시 invoke와 같은 Task의 중복 provisioning을 직렬화. 새 대화와 기존 Task 재사용을 양립시키고 scheduler run도 소유자 session에 연결. resume 시 최신 권한 재검사 |
| port 재사용 | 기존 Model/Retrieval/Response/Tool/Runtime/Policy/WorkRepository/Trace/Judge 유지. 세션은 우선 WorkRepositoryPort 확장; 독립 수명주기가 필요한 경우에만 SessionRepository 분리. SchedulerPort만 예약 경계에 추가. ResponsePort와 중복되는 ChannelAdapter는 P0에 만들지 않음 |
| 예약 | APScheduler **3.11.3** + SQLAlchemyJobStore(SQLite)를 구현 시 lock에 고정할 선택안. AsyncIOScheduler/CronTrigger 등 **3.x API만 사용**. 전용 단일 소유 프로세스, API worker별 시작 금지. 호환 dependency resolve와 재시작 검증 전에는 설치 완료가 아님 |
| 실행·외부 대응 | RuntimePort는 다영 OpenShell 모듈 교체 지점. local runtime은 sandbox 아님. Response/Tool은 승희 승인·MCP·게시 교체 지점. 승인 원본은 승희, runtime identity/강제 원본은 다영. 우리 mirror/mock은 그 권한을 가지지 않음 |
| 정책·증거 | PolicyPort의 결정적 정책이 필수, LLM 검수는 선택적 보조. 기밀 검수 최종 소유권은 미확정. 읽기·공유·cloud 전송을 각각 허가하고 미분류는 private. 팀원 DB/checkpoint 직접 공유 금지 |
| P0 보장 | 저장·상태 전이·권한·팀 중복 방지·예약·승인 버전 검증은 실제 local 코드. 모델·검색 공급자·실험·외부 응답은 mock 가능. `ALLOW_EXTERNAL_WRITES=true`여도 실제 외부 write 금지 |
| P1 주장 | NVIDIA 추론, 공식 Skill 실행, NemoClaw 운영, OpenShell 격리는 독립 task/evidence. 한 성공으로 다른 기술 활용을 대체하지 않음 |

핵심 데모는 **합성 노트/export → 권한 검색 → Benchmark/Research Task와 전담 팀 → 근거·미확인 항목·작업 후보 → owner/public DRAFT → 모의 검토/승인/게시 상태 → 예약 브리핑과 재시작 복구**다. `simulated`는 adapter/산출물별로 남겨 실제 local 제어 로직과 모의 실험 수치를 구분한다.

4일 필수 범위에서 제외: Engineering 실행, 실제 GPU benchmark, 자동 clustering 고도화, Debate, 추가 실시간 connector, 팀 재구성, OpenClaw 채널, production multi-tenant 인증, 분산 scheduler, 전체 RAG Blueprint/GPU 필수 배포. 기능 확장보다 권한 검사·승인 무효화·중복 방지·실제/모의 표시를 먼저 지킨다.

## 담당과 의존성 순서

| 담당 | 소유 범위 | 이 저장소 준비물 / 교체 지점 |
| --- | --- | --- |
| 민섭 | 비서·KB·Task 팀·DRAFT·세션·예약·본인 LLMOps | local 구현, DTO/port, mock, 결정적 검증, 통합 패키지 |
| 승희 | 채널 요청·MCP 실행·사용자 승인·실제 게시·receipt | `ResponseHttpAdapter`, `ToolHttpAdapter`, mapper, 인증 callback·대사 fixture |
| 다영 | OpenShell 환경·identity·권한 강제·UI | TeamSpec/AgentSpec, `RuntimeHttpAdapter`, UI polling 상태·이벤트 API |
| 미확정 | 기밀 검수 최종 서비스 | `PolicyPort`, `LocalPolicy`, `PolicyHttpAdapter`; 소유권 합의는 P0 선행 조건 아님 |

의존 순서는 계약/identity → 세션·checkpoint/KB → 라우팅 → 팀 선택·영속 팀·역할 실행 → 지식/후보/DRAFT → durable effect·예약 → reference 계약/평가/데모 → 실제 adapter 연결이다. 팀원 연락·답변·합의는 local 준비의 선행 task가 아니다. 모든 worker 위임과 결과 수집, 팀 내부/Task 간 통신은 Supervisor를 통한다.

## D1~D4 실행 계획

기술 고도화 추가 합의의 [작업 대응·용량·병렬 계획](EVALUATION_CONTEXT.md#설정과-일정--지원-설정으로-오인하지-않기)을 함께 적용한다. 아래 24h는 기존 코어 계획을 보존한 값이다. 신규 독립 5개 task는 4.5h 추가(직렬 총 28.5h)이며 별도 세션/검토 용량이 없으면 필수 작업 일부가 남는다. 같은 24h로 모든 추가 요구가 흡수됐다고 계산하지 않는다. NAT 설치 gate는 P0-028로 별도 집계하며 기존 final_acceptance P0-026은 중복 생성하지 않는다.

시간은 완료된 기반을 제외한 **민섭 잔여 집중 작업 시간**이며 카드별 구현과 최소 검증을 포함한다. 기존 DTO/SQLite/graph/test 재사용, 단일 PC·두 template·정해진 export·polling·결정적 mock에 한정한 최소 추정이다. 인프라 설치/외부 대기/큰 계약 변경은 포함하지 않으며 생기면 재산정한다. 안전 AC를 줄여 24시간을 맞추지 않는다. 초과 시 P1/P2와 시연 꾸밈부터 미루고 P0 미완료를 명시한다.

| 시점 | 순서와 시간 | 확인 가능한 결과 |
| --- | --- | --- |
| D1 — 8h | P0-014(1) → P0-015(1.5) → P0-016(1.5) → P1-001(1) → P1-001A(1.5) → P1-004(1) → P0-017(0.5) | 최소 DTO/reference 계약 동결·변경 이력, 입력→저장→권한 근거 답변, 세션 재조회·기본 resume |
| D2 — 8h | P0-018(0.5) → P0-019(1.5) → P0-020(1.5) → P1-004A(0.5) → P1-004B(1) → P2-003(0.5) → P1-005(1) → P1-005A(1) → P1-005B(0.5) | Task 팀 재사용·역할 결과·후보, owner/public DRAFT와 모의 승인 무효화 |
| D3 — 8h | P0-021(1.5) → P0-022(0.5) → P0-023(1.5) → P0-024(1) → P1-008(1) → P0-025(0.5) → P1-006(1) → P0-026(1) | 재시작/중복/누락 복구, reference 실패 계약, 핵심 12사례 평가 entrypoint(기존 24사례 보존, 추가 24행렬은 P1-006B), 전달 가능한 설정·fixture·실행 명령·데모 |
| D4 — 8h 조건부 | P1-002(1.5) → P1-002A(0.5), P1-003(1) → P1-003A(0.5), P1-008A(1), P1-008B(1), P1-007(0.5) → P1-007A(0.5), P1-007B(0.5), P1-009(1) | 준비된 상대 계약/환경 기준 실연동·UI와 경로별 증거. blocked 구간 시간은 로컬 회귀/실패 분석에 사용하며 실연동 완료로 대체하지 않음 |

D1~D3 핵심 합계 **24h**. D3 NVIDIA 범위는 카드/endpoint·설정·합성 입력 준비까지다. 현재 local 공백이 커 실제 Model/Skill adapter 및 호출은 D4로 배치했다(여유가 생기면 앞당김). 실제 Judge/Persona 시뮬레이션/Langfuse는 별도 본인 LLMOps **3.5h**, P2는 4일 밖이다. D4는 팀원 모듈과 실행 환경이 준비된 경우의 통합 시간이며 세 사람 전체 공수 합계가 아니다.

일정 주의: D1~D4는 상대 일차다. D1을 현재 날짜 2026-09-26에 시작하면 D4는 09-29이며, 제공된 공식 안내 기록의 제출 마감 **09-28 23:59 KST**를 넘는다. 마감 연장을 가정하지 않는다. 실제 제출에는 마감 전 확보된 증거만 사용하고, 일정 압축/기존 진행일 반영 없이는 위 전체 범위를 마감 내 완료한다고 약속하지 않는다.

## 기존 ID 변경 이력

| 기존 ID | 처리와 현재 우선순위 | 이유 / 연결 |
| --- | --- | --- |
| P0-001~P0-013 | done 유지 | 기반 범위 완료를 재개방하지 않음. 새 P0 완성은 후속 카드로 판단 |
| P1-001 | P1→P0, split | 입력/저장은 원 ID, ACL 최신 검색/무효화는 P1-001A. 외부 Retriever 없이 필수 흐름 구현 |
| P1-002 | P1 유지, split | adapter 준비는 원 ID, live 증거는 P1-002A. 기존 Lightning/Ultra는 후보 유지 |
| P1-003 | P1 유지, split | 공식 Skill worker/mapper는 원 ID, 실제 실행은 P1-003A. P0의 외부 선행 의존 제거 |
| P1-004 | P1→P0, split | 라우팅은 원 ID, 검토 지식은 P1-004A, 후보는 P1-004B. 모델/Skill 완료 의존 제거 |
| P1-005 | P1→P0, split | 대상/전송 정책은 원 ID, 편집·재승인은 P1-005A, 피드백은 P1-005B. 검수 소유권 확정을 AC에서 제거 |
| P1-006 | local 연결부 P0, split | 원 ID는 trace/규칙 runner; P1-006A/B/C는 실제 Judge/Persona 회귀/Langfuse. 보조 평가가 P0를 막지 않음 |
| P1-007 | P1 유지, split | 원 ID는 지원 경로 설계, P1-007A는 NemoClaw 실행, P1-007B는 OpenShell 강제. 별도 증거 |
| P1-008 | reference 준비 P0, split | 원 ID는 local 계약 완성; 승희 실연동은 P1-008A, 다영·UI 실연동은 P1-008B |
| P2-001/002/004 | P2, deferred | clustering/Debate/추가 connector는 핵심 이후. template 재구성은 P2-007로 분리 |
| P2-003 | P2→P0 | 설명 가능한 규칙 정렬은 핵심 후보 가치. 고급 Debate/LLM 비교는 P2-002/009 |
| 새 P0-014~P0-026 | P0 추가 | 계약·세션·영속 재개·팀·스케줄·복구·handoff의 구현 공백 |
| 새 P1-009, P2-005~P2-009 | 통합 gate / deferred | 최종 증거 점검과 제한된 확장. 기존 ID 삭제·다른 의미로 재번호화하지 않음 |

## 설정 후속 표 — P0-017에서 확정, 현재 추가된 설정 아님

실제 `.env`/`.env.dev`는 읽거나 덮어쓰지 않는다. 기존 `.env.example`와 Settings의 이름을 기준으로 한다. 아래 신규 이름은 **제안**이며 구현 시 의미 중복을 점검하고 typed settings·example·doctor·tests를 함께 맞춘다. 새 key/token은 모두 빈칸으로 둔다.

| 의미 | 기존 재사용 / 신규 예정 | 구현·검증 task |
| --- | --- | --- |
| 세션/메시지/Task/KB 저장 | `DATABASE_URL` 재사용, 별도 SESSION_DB 변수는 만들지 않음 | P0-015, P1-001 |
| LangGraph checkpoint | `CHECKPOINT_DATABASE_URL` 신규 예정; SQLite 경로·lifecycle·migration 책임 명시 | P0-016 |
| scheduler·job store | `SCHEDULER_BACKEND`, `SCHEDULER_ENABLED`, `SCHEDULER_JOBSTORE_URL` 신규 예정; 기본 자동 시작 false, 전용 runner만 소유 | P0-022, P0-023 |
| timezone·누락 정책 | `DEFAULT_TIMEZONE` 신규 예정(Asia/Seoul); 사용자 timezone은 DB. `SCHEDULER_MISFIRE_POLICY`는 승인된 job_type별 정책 선택자, 임의 실행 문자열 아님 | P0-024 |
| 총예산·동시성 | `MAX_GRAPH_STEPS`, `MAX_TOOL_CALLS`, `TOOL_TIMEOUT_SECONDS`, `HTTP_TIMEOUT_SECONDS`, `MAX_READ_RETRIES` 재사용. `MAX_TEAM_TOKENS`, `MAX_RUN_SECONDS`, `MAX_CONCURRENT_RUNS`, `MAX_CONCURRENT_WORKERS` 신규 예정, scope 중복 금지 | P0-020, P0-021 |
| provider·팀원·정책 | `MODEL_PROVIDER`, `NVIDIA_*`, `RETRIEVER_*`, `NEMO_RETRIEVER_API_TOKEN`, `RESPONSE_*`, `TOOL_*`, `RUNTIME_*`, `POLICY_*` 유지. 실제 주소/인증은 설정 주입, graph/source 상수에 넣지 않음 | P1-002/003/008A/008B |
| 평가·관측 | `ENABLE_JUDGE`, `JUDGE_*`, `TRACE_BACKEND`, `TRACE_DIR`, `LANGFUSE_*` 재사용; 평가 CLI 호출 자체를 opt-in으로 사용. `TRACE_RETENTION_DAYS` 신규 예정, redaction은 기본 강제 | P1-006, P1-006A/B/C |
| feature gate | `ALLOW_EXTERNAL_WRITES=false`, `ENABLE_DEBATE=false`, `ENABLE_AUTO_DOMAIN_CREATION=false` 유지. true만으로 정책 권한 생성 금지 | P0-017, P2-001/002 |

## 팀원 전달·교체 체크포인트

승희·다영에게 메시지를 보내거나 답변을 받는 행위는 이번 작업에 포함하지 않는다. D1 최소 계약, D3 전달 패키지를 먼저 완성한다. 실제 상대 스키마가 다르면 mapper/adapter만 바꾸며 core port의 업무 의미를 팀원 서비스 구현 세부사항으로 오염시키지 않는다.

| 경계 | 우리가 준비 / 현재 외부 의존 | 실제 교체 위치와 필수 증거 |
| --- | --- | --- |
| 승희 Response/Tool | DTO·OpenAPI·mock/rejected/revision/timeout/unknown·callback·receipt fixture. 실제 endpoint/OpenAPI/auth/approver/idempotency TTL/status·cancel 의미 미확인 | `src/rfa_mas/adapters/http.py`의 ResponseHttpAdapter/ToolHttpAdapter, 신규 teammate mapper, `bootstrap.py`. 승인 전 write 차단·수정 후 재승인·실제 receipt·timeout 대사 (`P1-008A`) |
| 다영 Runtime/UI | TeamSpec·멤버 AgentSpec·권한·예산·safe status/event API. 실제 identity/lifecycle/credential delivery/UI·registry schema 미확인 | RuntimeHttpAdapter와 mapper, `bootstrap.py`, `api/app.py`, `registry/service.json`. identity binding·cancel/cleanup·UI 시연 (`P1-008B`), sandbox 강제는 별도 `P1-007B` |
| 기밀 검수(소유권 미확정) | LocalPolicy + PolicyPort·공개 가능한 판단 사유 계약. 최종 검수 서비스/LLM 준비 불필요 | PolicyHttpAdapter·mapper. 결정적 deny를 원격/LLM 판단으로 완화하지 않는 테스트; 개인 선호와 공식 정책 변경 분리 |

기존 reference fixture의 bearer 미검증과 메모리 상태는 P1-008에서 보강할 공백이다. 원격 token이 설정되거나 HTTP adapter가 `simulated=false`를 반환한다는 이유만으로 상대 서비스 성공으로 기록하지 않는다. request/response·원본 상태·실행 환경을 실제로 대조해야 한다.

## 최종 데모와 완료 gate

fixture는 전부 합성이다. 기존 두 domain JSONL을 보존하고 [신규 예정] `fixtures/demo/materials.jsonl` 및 `fixtures/imports/`에 실험 메모, GitHub issue/Confluence export, 내부 잠정 계획, 공개 FAQ, 개인 1:1, 개인 GPU 자원 메모를 추가한다. 개인 자료 두 종류에는 독립 privacy canary를 둔다.

| 순서 | 시연·AC | task / 필요한 증거 |
| --- | --- | --- |
| 1 | 입력 자료 저장, 최신/충돌/미확정 근거·미해결 작업 정리 | P1-001/001A/004A/004B, source/revision·상태 조회 |
| 2 | Benchmark 팀 선택·역할 실행·통합, 같은 Task 후속 실행에서 기존 팀 재사용 | P0-018/019/020, TeamSpec·역할 결과·team count. 모의 실험 수치 `simulated=true` |
| 3 | owner는 허용된 내부 현황, 외부 channel은 public FAQ만으로 DRAFT | P1-004/005, 입력 EvidenceBundle과 출력 DTO의 허용 source 대조 |
| 4 | 승인 전 게시 없음, 편집/첨부/대상/권한 변경 후 낡은 승인 거절 | P1-005A/008, review·publication 별도 상태와 모의 receipt, 외부 write 0회 |
| 5 | 예약 briefing 1회, 재시작 후 세션·승인 대기 run·일정 재개, 동일 이벤트 부작용 중복 없음 | P0-016/021/023/024/026, 새 프로세스 증거·ledger·job history·public DRAFT/알림/오류/trace canary 검사 |
| 6 | 실제 NVIDIA 모델·공식 Skill·NemoClaw 경로·OpenShell allow/deny 각각 제시 | P1-002A/003A/007A/007B. 안 된 경로는 `not_run/미검증`, P0 mock으로 대체 금지 |

P0 완료는 1~5 및 해당 카드의 보안·상태·계약 회귀 통과다. P1 실제 활용 주장은 6의 경로별 증거가 있을 때만 가능하다. 좋은 LLM/Judge 점수가 권한·개인정보 gate 실패를 상쇄하지 않는다. artifact에는 command·시각·버전·mode·결과 ref를 기록하고 secret/header/private 본문은 제외한다. 지금 새 demo/성능/Judge 점수는 모두 **not_run**이다.

| 해커톤 평가 기준(배점 미공개) | 작업 연결 | 제출 시 보여줄 증거 |
| --- | --- | --- |
| NVIDIA Agent 기술 활용 심도 | P1-002A, P1-003A, P1-007A, P1-007B | 모델·Skill·NemoClaw·OpenShell 각각의 역할, 실제 실행·identity/policy·허용/차단 ref |
| 실용성·산업 가치·혁신성 | P1-001, P0-018~020, P1-004B, P2-003, P1-005 | 자료 축적→상황별 팀→근거 있는 업무 제안→대상별 안전한 응답 데모 |
| 완성도 | P0-015/016/021~026, P1-008A/B, P1-006, P1-009 | 설치·재시작·실패/취소/unknown·통합 계약·반복 가능한 demo·회귀 gate |
| 커스터마이징·독창성 | P0-018, P1-005B, P1-004A, P2-003; 완료한 P2만 추가 | 팀 template·개인 공개 선호·domain 지식·설명 가능한 정렬. 미구현 확장은 계획으로만 소개 |

## 공식 자료 확인과 남은 확인 항목

2026-09-26 KST 확인. 아래는 계획의 API/경계 근거이며 이 프로젝트의 실행 성공 증거가 아니다.

- [APScheduler 3.x user guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html): persistent job ID, coalescing/misfire, max_instances. [공식 PyPI 배포](https://pypi.org/project/APScheduler/3.11.3/)의 3.11.3을 선택안으로 잡았으며 현재 lock에는 아직 없다. [공식 FAQ](https://apscheduler.readthedocs.io/en/3.x/faq.html)의 job store 다중 프로세스 공유 제한에 따라 단일 owner로 설계했다.
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence): thread checkpointer와 장기 store 책임 분리, in-memory saver의 재시작 한계. 현재 프로젝트 버전과 SQLite async saver 조합은 P0-016에서 resolve/검증해야 한다.
- [공식 NVIDIA nemo-retriever SKILL.md](https://github.com/NVIDIA/skills/blob/main/skills/nemo-retriever/SKILL.md): 26.8.1 CLI, local LanceDB와 deployed service 경로, evidence 출력·source/page 보존 지침을 확인. 이 repo에서 설치/실행한 것은 아니다.
- [Lightning 모델 페이지](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b): 제공 예시의 endpoint와 model ID를 확인. tool calling·구조화 출력·한국어 품질·실제 계정 가용성은 P1-002/002A에서 따로 확인한다. Ultra 후보도 해당 시점 model card/API로 다시 확인한다.
- [NemoClaw 공식 overview](https://docs.nvidia.com/nemoclaw/latest/user-guide/openclaw/about/overview): 지원 runtime의 운영과 OpenShell 경계를 확인할 진입점. 임의 FastAPI/LangGraph 앱 자동 sandbox 수용의 증거로 사용하지 않는다.
- [해커톤 공식 폼](https://docs.google.com/forms/d/e/1FAIpQLScyZ5GYYaCOycNUzXVUTenliEUmSEIdXelVdYphvMvLeLuiHA/viewform)은 이번 도구 조회가 실패했다. 최신 변경을 확인했다고 주장하지 않으며 사용자 제공 2026-09-24 KST 안내를 기준으로 유지한다. 교육 [S-FX-43](https://learn.nvidia.com/courses/course-detail?course_id=course-v1:DLI+S-FX-43+V1) 페이지도 구체적 미션을 확인할 수 없어, 기존 교육 문서와 공식 공개 모듈을 참고하되 미확인 필수 조건을 추가하지 않는다. P1-009에서 제출 직전 재확인한다.

