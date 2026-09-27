# 팀원 레포 정합 (RFA_module · RequestForApproval)

확인 기준: RFA_module `819053f`(Step 9, 2026-09-27), RequestForApproval `a59a789`(2026-09-27).
`RFA_test`는 GitHub MCP 동작 확인용 빈 저장소(이슈 #2만 존재)이며 다영 님 작업은 `RequestForApproval`에 있다.
팀원 원본이 바뀌면 이 문서의 커밋 해시와 `fixtures/contracts/rfa_module/` 고정 사본을 함께 갱신한다.

## 1. 승희 — RFA_module (외부게시 MCP Agent)

| 승희 모듈 | 계약 | 이 저장소의 대응 | 상태 |
| --- | --- | --- | --- |
| `services/knowledge_stub` (8791) | `contracts/knowledge.openapi.yaml` — `GET /tasks`, `POST /tasks/{id}/ask` | **`rfa knowledge-facade`** (`src/rfa_mas/knowledge_facade/app.py`)가 같은 계약을 core 위에서 제공 | 구현·offline 검증. 실제 RFA_module 워크플로와의 연동 실행은 not_run |
| `services/review` (8790) | `contracts/review.openapi.yaml` — 결재 문서 상태기계, approve/reject loopback 전용 | `src/rfa_mas/adapters/rfa_module.py`의 `Review → RequestDetail` 매퍼(P1-011). ResponsePort 실제 어댑터 교체는 P1-008A gate | 매퍼만. 승인 원본은 승희 서비스 |
| `services/mcp_channels` (8792) | GitHub MCP 읽기 도구(list_mentions, get_thread) | 해당 없음(채널 소유 승희) | — |
| `workflow/rfa_workflow` | LangGraph Public 대응 그래프; writer가 knowledge 계약으로만 지식 획득 | facade가 "실무대장" 역할. 빈 `answer`는 그래프의 task 재선택/`returned` 규칙과 호환 | — |

### knowledge stub 교체 절차 (승희 `docs/modules/knowledge-stub.md` 순서 그대로)

1. 이 저장소에서 facade를 띄운다. 기본은 loopback·public audience다.

   ```sh
   uv run rfa knowledge-facade --host 0.0.0.0 --port 8791          # 다영 modules.yaml 형태
   uv run rfa knowledge-facade --port 8791 --audience company        # 사내 채널용; KNOWLEDGE_FACADE_API_KEY 필요
   ```

2. RFA_module `.env`의 `KNOWLEDGE_URL`을 facade 주소로 바꾼다. 워크플로 코드 변경은 없다.
3. 샌드박스 정책(`policies/rfa-host-services.yaml`의 `rfa_knowledge`)은 이미 `GET /tasks`, `POST /tasks/*/ask`만 허용하므로 그대로 쓴다.

### facade 동작 (core 포트 재사용, graph 변경 없음)

정책 결정(`PolicyPort`, action=retrieve, target=facade audience) → 허용 audience로 제한한 검색(`RetrievalPort`)
→ 기존 `share_egress_filter`(공유 가능 audience·private marker·cloud egress) → 모델 생성(`ModelPort`)
→ canary 치환 → `KnowledgeResult`. 근거가 하나도 남지 않으면 `answer=""`, `sources=[]`, `confidence=0`이다.

- `GET /tasks`는 현재 KB 도메인(`triv3`, `quantization_research`)을 승희 계약의 "task"로 투영한다. 해당 audience가 읽을 자료가 없는 도메인은 목록에서 빠진다. `updated_at`은 owner 자료의 최신 revision 날짜, 없으면 facade 시작일이다.
- `sources`는 승희 stub과 같은 "제목: 첫 줄" 형식이며 화면에 이미 통과한 근거만 담는다. `confidence`는 질문 용어가 근거에 나타난 비율(stub의 "겹침 비율"과 같은 의미)이지 정확도가 아니다.
- public facade는 호출자 인증이 없다(공개·공유 가능한 근거만 나가므로). `company`/`business_unit` audience는 `KNOWLEDGE_FACADE_API_KEY` bearer를 요구하며 키 없이는 시작을 거절한다. `X-RFA-Actor`는 기록용이며 권한이 아니다.
- 기밀 검토·초안·결재·게시는 승희 파이프라인 몫이다. facade는 승인하거나 게시하지 않는다. 승희 stub이 "기밀이 초안에 흘러가 censor가 거르는 장면"을 위해 일부러 섞는 자료는, 이 facade에서는 정책상 처음부터 나가지 않는다(데모 시나리오는 stub 데이터 또는 company audience + 사내 채널로 구성한다).
- 생성 OpenAPI: `docs/api/knowledge-facade.openapi.json` (`uv run rfa openapi --app knowledge-facade`). 승희 yaml과의 operation/schema 호환은 `tests/test_knowledge_facade.py`가 고정 사본으로 검사한다. 호출 시나리오는 [API 사용 시나리오](API_USAGE_SCENARIOS.md) 1장.

### 승희 review 계약과 core DRAFT/승인 모델의 대응

| RFA_module Review | rfa_mas | 비고 |
| --- | --- | --- |
| `opened → knowledge_ready → drafted → scanned → reviewed` | `RunResult.status=running` (P1-011 `in_progress`) | 워크플로 내부 단계 |
| `reviewed` (사람 결재 대기) | `waiting_approval` / 인박스 `needs_approval` | 결재 카드 표시 |
| `approved → posted` | `completed` + `PublicationReceipt(succeeded)` | `posted_url`이 receipt 참조 |
| `rejected` | `failed(rejected)` / 인박스 `declined` | 사유는 승희 feedback.jsonl |
| `needs_human` | 인박스 `needs_human` | 자동 복구 한도 초과 |
| `approve/reject` loopback 전용 | 사람만 결정; 에이전트 route allowlist에 없음 | 다영 `deny_rules`와 이중 차단 |
| clearance HMAC(review_id, target, sha256(body), exp) | `DraftBinding`(payload_hash·target·policy) | 둘 다 본문/대상 변경 시 승인 무효 |

## 2. 다영 — RequestForApproval (NemoClaw/OpenShell 운영)

| 다영 산출물 | 내용 | 이 저장소의 대응 |
| --- | --- | --- |
| `modules.yaml` | 모듈 레포·ref·포트·실행 명령. knowledge 8791은 "민섭 구현으로 교체 예정" | `uv run rfa knowledge-facade --host 0.0.0.0 --port 8791`이 그 실행 명령이 된다. 교체 후 `ref`를 이 저장소 커밋으로 갱신 요청 |
| `docs/ports.md` | 8080 게이트웨이, 18789 대시보드, 8790/8791/8792 호스트 서비스, `inference.local`, 프록시 10.200.0.1:3128 | core API(8000)는 아직 샌드박스 허용 목록에 없다. 필요하면 `docs/INTEGRATION.md`의 agent 허용 4 route(`/healthz`, `POST /v1/sessions`, `POST /v1/sessions/{id}/work`, `GET /v1/runs/{id}`)만 추가 |
| `policies/rfa-host-services.yaml` | `rfa_review`/`rfa_knowledge` REST 규칙, approve/reject/republish·`POST /policy/**` deny | facade는 허용 두 경로만 노출하므로 추가 정책 불필요 |
| `agents/rfa.yaml` | public-desk 에이전트 허용 도구 3개, main 잠금 | 이 저장소의 로컬 runtime은 sandbox가 아니다(README "실제 vs mock"). OpenShell 실행 증거는 P1-007C, 역할별 identity는 P1-007B(blocked) |
| `docs/proposal.md`, `ideation_handoff.md` | 제안서·아이디에이션 | 제출 문서(P1-009)와 교차 참조 |

주의: 다영 문서는 `0.0.0.0` 바인딩의 LAN 노출 위험을 명시한다. facade의 public audience는 공개 근거만 내보내므로 키 없이 바인딩해도 되지만, 그 외 audience는 loopback 또는 bearer 키를 요구한다.

## 3. 결재 인박스 UI PoC 대응 API (P1-011)

UI 시안(A · 메일형 3단 + 관리자)의 화면 항목을 `/v1/inbox` 계약으로 정의했다. 생성 OpenAPI는
`docs/contracts/inbox.openapi.yaml`, PoC 데이터로 동작하는 reference 서버는 `python -m rfa_mas.reference.inbox`다.
각 operation의 `x-rfa-authority`가 실제 원본(승희 review · 다영 runtime · 민섭 core)을 표시한다. 자세한 대응표는
[inbox 계약 설명](INBOX_API.md)에 있다.
