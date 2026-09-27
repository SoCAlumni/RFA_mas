# API 사용 시나리오 (OpenAPI별)

이 문서는 `docs/api/`의 생성 OpenAPI 두 개를 실제 호출 흐름 단위로 설명한다. 스키마의 원본은
Pydantic 모델이며 OpenAPI와 이 문서는 파생물이다.

| OpenAPI | 제공자 | 소비자 | 실행 | 상태 |
| --- | --- | --- | --- | --- |
| [knowledge-facade.openapi.json](api/knowledge-facade.openapi.json) | rfa_mas core (민섭) | 승희 RFA_module 워크플로(writer) | `uv run rfa knowledge-facade --host 0.0.0.0 --port 8791` | 실제 로컬 코드. 모델은 설정된 provider(기본 mock) |
| [inbox.openapi.yaml](api/inbox.openapi.yaml) | reference 서버(PoC 표본) → 실제 원본은 route별 `x-rfa-authority` | 다영 결재 인박스 UI | `uv run python -m rfa_mas.inbox --port 8793` | reference/mock. 승인·게시·샌드박스 제어 없음 |

core API(8000, `uv run rfa openapi`)는 기존 [INTEGRATION.md](INTEGRATION.md)를 따른다.

---

## 1. knowledge-facade — 실무대장 계약 (`GET /tasks`, `POST /tasks/{task_id}/ask`)

승희 계약 `contracts/knowledge.openapi.yaml`(819053f)과 1:1이다. 호출 순서는 승희 워크플로의
`ask_knowledge` 노드가 정한다: 목록 조회 → LLM이 task 선택 → 질문 → 빈 답이면 다른 task로 한 번 재선택 → 그래도 없으면 `returned`.

### 1.1 `listTasks` — 지금 살아 있는 task 목록

시나리오: 워크플로가 멘션 "@zetwhite ORBIT 벤치마크 진행 어때?"를 받고 어느 task에 물을지 고르기 위해 목록을 가져온다.

```sh
curl -s http://127.0.0.1:8791/tasks
```

```json
[
  {"id": "triv3", "name": "TRIV3 벤치마크",
   "description": "TRIV3 합성 벤치마크 트랙의 진행·결과·근거 (공개 fixture 기준)", "updated_at": "2026-09-27"},
  {"id": "quantization_research", "name": "Quantization Research",
   "description": "양자화 연구 비교 기준·실험 노트 (공개 fixture 기준)", "updated_at": "2026-09-27"}
]
```

- 목록은 core KB 도메인을 task로 투영한 것이다. facade audience(기본 public)가 읽을 자료가 하나도 없는 도메인은 목록에서 빠지므로, 목록에 있다는 것은 "물어보면 최소 한 건은 답할 수 있다"는 뜻이다.
- `name`/`description`은 승희 `pick_task.md` 프롬프트가 그대로 읽는다. 사내 명칭을 넣지 않는다.
- `updated_at`은 owner 자료의 최신 revision 날짜, 없으면 facade 시작일이다.

### 1.2 `askTask` — task supervisor에게 질문

시나리오 A · 정상 답변: 워크플로가 `triv3`를 고르고 질문한다. 답과 근거 한 줄씩을 받아 `POST /reviews/{id}/knowledge`에 첨부한다.

```sh
curl -s -X POST http://127.0.0.1:8791/tasks/triv3/ask \
  -H 'Content-Type: application/json' -H 'X-RFA-Actor: knowledge' \
  -d '{"question": "TRIV3 벤치마크 트랙 진행 상황을 알려줘"}'
```

```json
{"task_id": "triv3",
 "answer": "요청 요약: 공개 대상에 맞춰 허용된 근거만 요약합니다.\n\n허용된 근거:\n- TRIV3는 이 데모를 위해 만든 합성 벤치마크다. ...",
 "confidence": 0.5,
 "sources": ["TRIV3 합성 벤치마크 공개 개요: TRIV3는 이 데모를 위해 만든 합성 벤치마크다. 세 트랙은 ..."]}
```

- `sources`는 "제목: 첫 줄" 형식으로, 정책·공유·egress 필터를 통과한 근거만 담긴다. editor의 근거 대조와 censor의 출처 추적에 그대로 쓴다.
- `confidence`는 질문 용어가 근거에 나타난 비율이다(승희 stub의 "겹침 비율"과 같은 의미). 사실 정확도가 아니다.
- `X-RFA-Actor`는 기록용이며 권한을 바꾸지 않는다.

시나리오 B · 근거 없음 → 재선택: 관련 자료가 없거나 정책이 거절하면 빈 답을 준다. 승희 `ask_knowledge`는 `answer.strip()==""`를 "지식 없음"으로 보고 다른 task로 한 번 더 고른다(복구 예산 1).

```json
{"task_id": "triv3", "answer": "", "confidence": 0.0, "sources": []}
```

시나리오 C · 비공개 자료가 섞인 도메인: owner 메모(canary 포함)·팀 내부·사내 자료가 같은 도메인에 있어도 public facade 응답에는 나타나지 않는다. 승희 stub이 데모용으로 기밀을 초안에 흘리는 장면은 이 facade에서는 정책상 재현되지 않으므로, 그 데모는 stub 데이터나 `--audience company` + 사내 채널로 구성한다.

시나리오 D · 질문 자체에 private marker가 있을 때: 외부 채널 질문에 `SYNTHETIC_PRIVATE_CANARY_*` 같은 marker가 있으면 writer가 이미 갖지 말아야 할 것을 들고 있는 것이므로 빈 답을 준다.

시나리오 E · 없는 task: `POST /tasks/orbit/ask` → `404 {"error": "not_found", "id": "orbit"}`. 워크플로는 목록에 없는 id를 고르지 않으므로 정상 흐름에서는 나오지 않는다.

시나리오 F · 사내 채널(company audience): `uv run rfa knowledge-facade --port 8791 --audience company`로 띄우면 `KNOWLEDGE_FACADE_API_KEY`가 필수이며 모든 호출에 `Authorization: Bearer <키>`가 필요하다. 없으면 `401 {"error": "authentication_required"}`. 이때도 owner/팀 내부 자료는 나가지 않는다.

시나리오 G · 형식 오류: `question`이 비어 있으면 422. 본문의 추가 필드(hint 등)는 무시한다.

### 1.3 전체 흐름에서의 위치

```text
GitHub 멘션 → public-desk(list_mentions) → workflow.run
  → POST /reviews (opened)
  → GET /tasks → POST /tasks/{id}/ask  ← 이 facade
  → POST /reviews/{id}/knowledge (knowledge_ready) → writer/editor → POST /reviews/{id}/draft (scanned)
  → censor → POST /reviews/{id}/verdict (reviewed) → 사람 결재(인박스) → approve → 게시
```

facade는 승인·게시를 하지 않는다. 승희 review 서비스가 결재 원본이고, 다영 샌드박스 정책은 `GET /tasks`, `POST /tasks/*/ask`만 허용한다.

---

## 2. inbox — 결재 인박스 UI 계약 (`/v1/inbox`)

reference 서버 기준 예시다. 각 route의 `x-rfa-authority`가 실제 원본을 가리키며, 실제 백엔드로 교체돼도 응답 스키마는 같다.
`DecisionReceipt.upstream.authority`가 `reference`이면 아무것도 실제로 일어나지 않은 것이다.

### 2.1 첫 화면 렌더링 — `getCurrentApprover`, `listInboxes`, `getCounts`

시나리오: 다영 님이 결재 인박스를 연다. 좌측 레일과 배지를 한 번에 그린다.

```sh
curl -s http://127.0.0.1:8793/v1/inbox/me        # {"display_name":"이다영","role":"admin","sandboxes_running":3,"sandboxes_total":4,...}
curl -s http://127.0.0.1:8793/v1/inbox/inboxes   # GitHub 이슈·Slack 문의·내 리서치·이메일 + pending_count
curl -s http://127.0.0.1:8793/v1/inbox/counts    # {"needs_approval":5,"notifications_unread":4,"all_inboxes":5}
```

- `sandboxes_running`은 샌드박스 상태에서 계산한다(division이 꺼져 있어 3).
- `InboxSummary.clearance`(public/company/division/team)로 등급 라벨을 표시한다. 등급이 높을수록 읽는 사람이 적다.

### 2.2 요청 목록 — `listRequests`

시나리오 A · 기본 목록(전체 탭, 최신순): `GET /v1/inbox/requests` → `RequestPage.items`와 `counts`.

```json
{"request_id": "req-34", "inbox_id": "github-issues",
 "requester": {"requester_id": "outside-dev", "display_name": "outside-dev", "initials": "OD", "kind": "external"},
 "title": "ORBIT 벤치마크 진행 어때?", "subtitle": "결재 12 · 어디까지 공개할까요?",
 "status": "needs_approval", "approval_id": "approval-12", "received_at": "2026-09-27T08:40:00+09:00", "is_new": false}
```

| 화면 동작 | 쿼리 |
| --- | --- |
| "결재 필요" 탭 | `?status=open` (needs_approval·blocked·held·needs_human) |
| "자동응답" 탭 | `?status=auto_replied` |
| 인박스 하나만 | `?inbox_id=my-research` |
| 검색(Ctrl K) | `?q=PRISM` (제목·부제·요청자 부분 일치) |
| 정렬 | `?sort=received_asc` |

- 상태 배지는 `status`로 그린다: `blocked`=차단됨, `needs_approval`=결재 필요, `auto_replied`=자동응답, `decided`/`declined`=결재 완료, `held`=보류, `in_progress`=처리 중, `needs_human`=사람 판단 필요.
- `subtitle`은 서버가 만든 두 번째 줄이다. 결정 뒤에는 "결재 12 · 하락 방향까지로 응답"처럼 바뀐다.

### 2.3 요청 상세 — `getRequest`

시나리오 A · GitHub 이슈(결재 12): 원본 이슈 본문, 댓글 자리 표시, 결재 카드, 하단 차단 알림까지 한 응답에 들어 있다.

```json
{"decision": {"approval_id": "approval-12", "asked_by": "public-desk", "kind": "disclosure_scope",
  "question": "어디까지 공개할까요?", "note": "항상 빼는 것: 모델 이름, 출시 날짜, 사내 자원 주소",
  "always_excluded": ["모델 이름", "출시 날짜", "사내 자원 주소"],
  "options": [
    {"option_id": "progress-only", "label": "진행 사실만", "description": "개선 작업 중이라고만 답합니다.", "recommended": false, "badge": null, "disclosure_level": 1},
    {"option_id": "direction", "label": "하락 방향까지", "description": "소폭 하락했다고 알리고 수치는 뺍니다.", "recommended": true, "badge": null, "disclosure_level": 2},
    {"option_id": "numbers", "label": "수치까지", "description": "EM 0.5%p 하락을 그대로 알립니다.", "recommended": false, "badge": "내부 지표", "disclosure_level": 3}],
  "primary_label": "이 범위로 응답", "secondary_label": "응답하지 않기", "secondary_action": "decline",
  "allow_rule": true, "preview_available": true},
 "alerts": [{"kind": "sandbox_blocked", "message": "샌드박스가 이 결재를 직접 승인하려다 차단됐습니다.", "at": "2026-09-27T08:55:00+09:00", "activity_id": "act-1201"}]}
```

- 버튼 문구는 서버가 준 `primary_label`/`secondary_label`을 쓴다(GitHub "응답", Slack "답장", 리서치 "진행"). `secondary_action`이 `hold`이면 두 번째 버튼은 보류다.
- `recommended`는 최대 하나다. `badge`("내부 지표", "기밀 문서")는 경고 표시다.
- `alerts[].activity_id`로 "활동에서 보기" 링크를 `GET /v1/inbox/activity?request_id=`에 연결한다.

시나리오 B · 주입 공격 차단(req-36): `original.messages[0].flagged_spans`로 명령문을 강조하고 `injection_warning`을 아래에 표시한다. `decision.kind`는 `blocked_handling`, `blocked_attempts`가 "막은 시도 3건" 표다.

```json
{"start": 22, "end": 77, "reason": "질문이 아니라 에이전트에게 내리는 지시로 보입니다."}
{"at": "2026-09-27T09:02:00+09:00", "action": "POST /reviews/14/approve", "reason": "결재는 사람만 할 수 있음", "kind": "review_approve", "activity_id": "act-1401"}
```

시나리오 C · Slack DM(req-slack-prism): `original.source.kind="slack_dm"`, 메시지 2건, `decision.kind="share_scope"`, 세 번째 옵션에 `badge="기밀 문서"`.

시나리오 D · 리서치 터미널(req-orbit-regression): `original.terminal[]`(command/reading/hypothesis/evidence/approval/waiting)로 로그를 그리고, `decision.kind="research_direction"`, `effect_note="고른 방향은 다음 실행에 바로 반영됩니다"`, `evidence_link_label="근거 노트 보기"`.

시나리오 E · 이미 끝난 요청(req-32 자동응답, req-customer-sdk 거절): `decision`은 null이고 `outcome`에 누가·언제·어떤 규칙으로 결정했는지 남는다.

### 2.4 결정 제출 — `submitDecision`

시나리오 A · 범위 선택 + 규칙 저장(결재 12): "하락 방향까지"를 고르고 체크박스를 켠 뒤 "이 범위로 응답".

```sh
curl -s -X POST http://127.0.0.1:8793/v1/inbox/requests/req-34/decision \
  -H 'Content-Type: application/json' \
  -d '{"approval_id":"approval-12","action":"respond","option_id":"direction","save_as_rule":true,"idempotency_key":"ui-2026-09-27-001"}'
```

```json
{"request_id": "req-34", "approval_id": "approval-12", "status": "decided",
 "outcome": {"action": "respond", "option_id": "direction", "decided_by": "dayoung", "decided_at": "2026-09-27T05:21:23Z", "rule_id": "rule-101"},
 "rule": {"rule_id": "rule-101", "inbox_id": "github-issues", "kind": "disclosure_scope", "action": "respond", "option_id": "direction",
          "created_from_approval_id": "approval-12", "description": "'ORBIT 벤치마크 진행 어때?'와 비슷한 요청은 하락 방향까지로 처리", "applied_count": 0, "enabled": true},
 "upstream": {"authority": "reference", "reference": null, "simulated": true}}
```

UI는 응답의 `status`로 배지를 바꾸고, 알림함 배지를 `getCounts`로 갱신한다. 같은 `idempotency_key`로 다시 보내면 같은 receipt가 돌아오고(더블 클릭 안전), 같은 key에 다른 본문은 409.

시나리오 B · 응답하지 않기: `{"approval_id":"approval-11","action":"decline"}` → `declined`, 부제 "답장하지 않기로 결정".

시나리오 C · 보류 후 재결정(결재 13): `action:"hold"` → `held`(카드 유지) → 나중에 `respond`+`option_id:"qat"` → `decided`, 부제 "QAT 적용로 진행".

시나리오 D · 차단 처리(결재 14, "이대로 처리"): 카드 kind가 `blocked_handling`일 때 옵션이 곧 처리 방법이다.

| 선택 | 요청 | 결과 |
| --- | --- | --- |
| 응답하지 않기(추천) | `respond`+`no-reply` 또는 `decline` | `declined` |
| 질문에만 답하기 | `respond`+`answer-question-only` 또는 `answer_question_only` | `in_progress` (에이전트가 공개 범위 결재를 다시 올림) |
| 작성자 요청 받지 않기 | `respond`+`block-author` 또는 `block_author` | `declined` (+규칙 저장 시 작성자 차단 규칙) |

`answer_question_only`/`block_author`를 차단 카드가 아닌 요청에 보내면 422.

시나리오 E · 오류: 카드가 바뀐 뒤 옛 `approval_id`로 보내면 `409 stale_approval`(상세를 다시 불러 재표시), 이미 끝난 요청은 `409 invalid_state`, 없는 option은 `422 invalid_option`, `respond`인데 `option_id`가 없으면 422.

실제 원본으로 교체될 때: `rfa_module.review` authority 요청은 호스트가 승희 review 서비스에 `approve`(게시본 승인) 또는 `reject`(reason에 `scope:<option>`/`hold`/`decline`)를 loopback으로 호출한다(`src/rfa_mas/inbox/rfa_module.py`의 `to_review_call`). 3단계 범위의 재작성은 승희 Step 12+ 이후다. `research_direction` 카드는 core의 팀 run 재개로 연결된다.

### 2.5 응답 전문 미리보기 — `previewReply`

시나리오: 옵션 카드를 고른 상태에서 "응답 전문 미리보기" 링크를 누른다. 전송이 아니다.

```sh
curl -s 'http://127.0.0.1:8793/v1/inbox/requests/req-34/preview?option_id=direction'
```

`excluded`(항상 빼는 것)를 미리보기 아래에 같이 보여 준다. 승희 review 매핑에서는 `as-reviewed`가 `final_body`, `original-draft`가 검토 전 `draft`다. 미리보기가 없는 옵션은 404.

### 2.6 알림함 — `listNotifications`, `markNotificationRead`

시나리오: 알림함 4를 누르면 최신순 목록. 항목을 열면 `request_id`로 상세로 이동하고 `POST /v1/inbox/notifications/{id}/read`로 읽음 처리. 결정이 끝나면 서버가 `kind="decided"` 알림을 추가한다. `?unread_only=true`로 안 읽은 것만 가져온다.

### 2.7 활동 기록 — `listActivity`

시나리오: "활동 기록에서 보기"/"활동에서 보기" 링크. `?request_id=req-36&outcome=blocked`로 그 요청에서 샌드박스가 막은 호출만 본다. 관리자 화면의 활동 기록 탭은 필터 없이 최신순이다. 실제 원본은 다영 runtime(프록시/게이트웨이 로그)이다.

### 2.8 규칙 — `listRules`, `deleteRule`

시나리오: "결재 9의 규칙으로 자동응답" 행을 눌러 어떤 규칙인지 확인하고, 잘못 만든 규칙은 삭제한다(204). 규칙은 `submitDecision`의 `save_as_rule`로만 생성된다. 승희 계약의 feedback.jsonl(거절 사유 축적)과는 별개의 core 개념이다.

### 2.9 관리자 · 에이전트 — `listAgents`, `getAgent`, `toggleAgentSource`, `compactAgentConversation`, `clearAgentMemory`

시나리오 A · 목록/상세: 에이전트 5개(인박스·샌드박스·등급·상태·오늘 호출 수). `task-orbit` 상세는 컨텍스트 5,912/8,192 토큰과 구성비, 불러오는 자료 3건, 호출 통계(38회·205천 토큰·2.9초·차단 0)와 7일 그래프.

시나리오 B · 자료 토글: `PUT /v1/inbox/admin/agents/task-orbit/sources/orbit-progress {"enabled": false}`. 다른 구획 자료(PRISM 설계 문서)는 `409 not_selectable`이며 `unavailable_reason`을 그대로 표시한다.

시나리오 C · 대화 압축 / 기억 비우기: `POST .../compact`는 대화 기록 토큰을 0으로, `POST .../clear-memory`는 기억·노트까지 0으로 만든 상세를 돌려준다. 실제 원본은 다영 runtime(OpenClaw 세션)이다.

### 2.10 관리자 · 샌드박스 — `listSandboxes`, `getSandbox`, `updateSandboxSettings`, `applySandboxSettings`

시나리오 A · 목록/상세: 등급마다 하나(public/company/division/team). `team` 상세:

```json
"gateway": {"gateway_id": "gw-confidential", "label": "기밀용 · 포트 8090", "port": 8090, "providers": ["ollama-local"],
            "has_external_key": false, "control_port": 18791, "current_route": "ollama-local / qwen3.5:9b",
            "shared_with": ["company", "division", "team"]}
"provider_options": [
  {"provider": "ollama_local", "label": "Ollama 로컬", "selectable": true, "reason": "프롬프트가 이 PC 밖으로 나가지 않습니다."},
  {"provider": "nvidia_endpoints", "label": "NVIDIA Endpoints", "selectable": false, "reason": "공개 등급 샌드박스에서만 쓸 수 있습니다."}]
```

`shared_with`는 "게이트웨이 하나에는 추론 경로가 하나뿐 · 같은 게이트웨이를 쓰는 샌드박스도 함께 바뀜" 안내에 쓴다.

시나리오 B · 설정 변경 → 적용: `PATCH`는 저장만 하고 `pending_changes=true`가 된다("바뀐 설정이 없습니다" 문구는 false일 때). "변경 적용"이 `POST .../apply`.

```sh
curl -s -X PATCH http://127.0.0.1:8793/v1/inbox/admin/sandboxes/team \
  -H 'Content-Type: application/json' -d '{"policies": {"github": true}, "context_length": 16384}'
curl -s -X POST http://127.0.0.1:8793/v1/inbox/admin/sandboxes/team/apply
```

```json
{"sandbox_id": "team", "applied": ["provider", "model", "max_output_tokens", "policies"],
 "requires_recreate": ["context_length", "gateway"], "upstream": {"authority": "reference", "reference": null, "simulated": true}}
```

`requires_recreate`는 "다시 만들 때 적용" 항목이다. "되돌리기"는 서버 호출 없이 상세를 다시 불러오면 된다(PATCH 전 값은 `GET`으로 복구할 수 없으므로 UI가 원본을 들고 있다가 되돌린다).

시나리오 C · 거절: 공개 등급이 아닌 샌드박스에 `provider: nvidia_endpoints` → `409 not_selectable`; 없는 모델/컨텍스트 길이 → 422; 없는 게이트웨이/정책 id → 404. 실제 원본은 다영 NemoClaw(`nemoclaw <sandbox> policy add`, 게이트웨이 설정)이다.

### 2.11 authority별 교체 계획

| authority | route | reference에서 실제로 바뀌는 것 |
| --- | --- | --- |
| `rfa_module.review` | listRequests, getRequest, submitDecision | 승희 `GET /reviews`를 `review_to_request`로 변환, 결정은 호스트 loopback에서 approve/reject |
| `runtime.sandbox` | listActivity, admin agents/sandboxes 전부 | 다영 runtime 조회·제어 어댑터. 표시 스키마는 동일 |
| `core` | me, inboxes, counts, preview, notifications, rules, toggleAgentSource | 이 저장소 core API/DB |

교체 뒤 `DecisionReceipt.upstream`은 `{"authority": "rfa_module.review", "reference": "review:12", "simulated": false}`처럼 실제 참조를 담는다.

---

## 3. 검증 명령

```sh
uv run python -m pytest -q tests/test_knowledge_facade.py tests/test_inbox_reference.py
uv run python scripts/export_openapi.py            # docs/api 재생성(내용이 바뀌면 커밋)
```
