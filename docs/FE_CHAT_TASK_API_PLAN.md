# 프런트 연동 1차: 「검색하거나 물어보기」 · 「태스크 추가」

- 범위: 이 두 기능만. 결재함·알림함·결재 상세·관리자 화면은 다루지 않는다.
- 기준 UI: `RFA · 결재 태스크 UI + 비서 채팅` 시안 (artifact `LFD3Utajj2ahC9wv45MbAg`, 2026-09-28 2차 판)
- 시안의 목 백엔드는 `runQuery(req, signal) → AsyncIterable<Event>` 한 함수다. 이것을 실제 SSE 로 바꾸는 것이 중심이다.
- 등급(D-0.4): 사내/사외는 요청마다 매긴다. 태스크·에이전트에는 등급이 없다. 채팅 질문 1건 = 요청 1건이고 역할이 정한다(owner → 사내, guest → 사외).
- 상태: 결정 완료(2026-09-28, `docs/decisions.md` D-5·D-6·D-7·D-10·D-12~D-15), 1~4단계 구현(WORK_LOG FE-5). 계약 원본은 `docs/openapi.yaml`.

---

## 1. 「검색하거나 물어보기」

### 1.1 요구사항

| ID | 요구사항 |
|---|---|
| C-1 | 진입: 좌측 「검색하거나 물어보기」 → `#chat`(비서와 대화). 태스크 항목의 말풍선 버튼 → `#chat/{agentId}`(그 에이전트 지정). 지정 대화도 비서가 받아 그 에이전트에게 확인한 뒤 답한다. |
| C-2 | 대화 상대 목록 = 비서 + 태스크 에이전트 전부(검열 제외). 카드: 아이콘·색, 이름, 부제(비서 「담당자 N명을 관장」, 태스크 에이전트 「{상태} · {태스크명}」), 설명. 상태 점(`running`), 답변 중이면 스피너. |
| C-3 | 「바꾸기」 메뉴: 에이전트마다 최신 대화 제목 또는 「답변 중…」, 마지막 대화 시각. |
| C-4 | 「대화 내역」: 현재 에이전트의 대화(사용자 메시지 1개 이상), 최신순, 제목 = 첫 질문. 「+ 새 대화」. 에이전트를 바꾸면 마지막으로 연 대화 → 최근 대화 → 새 대화 순으로 연다. |
| C-5 | 여러 대화가 동시에 답변을 받을 수 있다. 대화 하나에는 동시에 한 질문만. |
| C-6 | 헤더: 이름, 부제(「{상태} · {태스크명} · 비서 에이전트를 통해 대화」 / 「대화 가능 · 담당자 N명을 관장」), 「새 대화」. |
| C-7 | 빈 화면: 비서는 「무엇을 확인해 드릴까요?」 + 전역 추천 질문 4개. 지정 에이전트는 프로필(태스크명, 상태, 「맡은 안건 N건」, `#태그`) + 그 에이전트 추천 질문. 누르면 바로 전송. |
| C-8 | 비서 답변의 진행 과정 4단계: ①요청 파악(서두 스트리밍) ②담당자 탐색(후보·이유·점수, 없으면 「맞는 담당자가 없습니다. 비서가 직접 답합니다.」) ③담당자 호출(상태 찾음/응답 기다리는 중/답 받음 · 1.4초/해당 없음/샌드박스 정책으로 차단됨/실패, 펼치면 위임 내용·로그·요약·참조 칩) ④등급 검사(「{사외\|사내} 등급으로 검사 중」 → 통과 / N건 제외 / 차단 + 메모). |
| C-9 | 답변: 스트리밍, 끝나면 참조 칩 모음. 꼬리: 완료 「3.9초 · 사내 등급 답변」, 중지 「중지했습니다 · 다시 물어보기」, 실패 「답변하지 못했습니다: {오류} · 다시 시도」. |
| C-10 | 입력: Enter 전송, Shift+Enter 줄바꿈, 실행 중에는 「중지」. 안내 「대화 내용은 LLM API Endpoints로 추론합니다. 답변은 등급 검사를 거쳐 나옵니다.」 (「안건 붙이기」·「@」 버튼은 시안에 동작 없음 → 제외) |
| C-11 | 담당자가 필요 없는 일반 질문은 비서가 바로 답한다. |
| C-12 | 등급: owner → 사내, guest → 사외. 게스트는 호출이 차단되거나 답변 일부가 제외될 수 있다. |

### 1.2 스트림 이벤트 (시안 리듀서 기준)

| type | data |
|---|---|
| `run.start` | `{conversationId, messageId, role, agentId, level}` |
| `assistant.delta` | `{text}` — 요청 파악 서두 |
| `agents.search` | `{query, candidates:[{agentId, reason, score}]}` |
| `agents.select` | `{selected:[{agentId, task}], skipped:[{agentId}]}` |
| `delegate.start` | `{callId, agentId, task}` |
| `delegate.log` | `{callId, text}` |
| `delegate.end` | `{callId, status: ok\|none\|blocked\|error, summary, refs, durationMs}` |
| `guard.start` | `{level: public\|company}` → 사외/사내 |
| `guard.end` | `{status: pass\|redacted\|blocked, note, redactions}` |
| `answer.delta` | `{text}` |
| `run.end` | `{status: done\|stopped\|error, durationMs, error?, messageId}` |

봉투는 기존 `{type, runId, seq, ts, data}`. 중지 = 클라이언트가 연결을 끊음(서버는 실행 취소).

흐름 규칙:
- 담당자 탐색(D-12): head 가 질문(+대화 context)에 대해 task 마다 점수를 매겨 `agents.search.candidates` 로 보낸다. `score ≥ head.select_threshold`(기본 0.6)인 후보를 점수순 최대 `head.max_parallel`(기본 4)개 `agents.select.selected` 로 고르고 나머지는 `skipped`. 선택된 담당자는 병렬로 `delegate.*` 가 흐르고(callId 로 구분), 비서가 담당자 답만 근거로 합친다. 1명이면 합치기 없이 그 답을 쓴다.
- 지정 대화(D-5): `agentId` 가 있으면 head 없이 후보 1개(score 1, reason 「지정한 담당자」)로 바로 위임.
- 담당자 없음(D-13): 선택된 후보가 없으면 비서가 직접 답한다. 근거 없는 사내 정보는 추측하지 않고 「알 수 없습니다」로 답한다. 담당자가 `none`(근거 없음)을 돌려준 경우도 같다.
- 사외 질문의 진행 과정(D-14): `delegate.log`·`delegate.end.summary` 는 정해진 문구(「{이름}에게 위임」「응답 수신 · N자」)만. 원문 요약은 사내 질문에만.
- `delegate.log`(D-15): 서버 단계 로그(위임 시작, 샌드박스 경로, 응답 수신, 실패 사유).

지금 `/chat` 과의 대응: `run.started`→`run.start`, `stage head`→`agents.search`+`agents.select`, `stage task`→`delegate.*`(no_knowledge=`none`, blocked_by_policy=`blocked`), `stage censor`·`guard.final`→`guard.*`(소유자에게도), `delta`→`answer.delta`, `done`·`error`→`run.end`. `assistant.delta` 는 새로 만든다.

---

## 2. 「태스크 추가」

| ID | 요구사항 |
|---|---|
| T-1 | 소유자만. 게스트는 「소유자만 할 수 있어요」 안내, 서버도 403. |
| T-2 | 안내: 「태스크와 에이전트는 1:1로 만들어집니다. 사내·사외 등급은 태스크가 아니라 들어오는 결재 대상마다 매겨집니다.」 → 등급 선택 없음. |
| T-3 | 필드: 태스크명(필수 ≤30), 에이전트 이름(필수 ≤40, 비면 「{태스크명} 대응 에이전트」), 설명(선택 ≤200, 비면 「{태스크명} 태스크를 맡은 에이전트입니다.」), 태그(≤8, `#` 제거·중복 제거). |
| T-4 | 검증 문구: 「태스크명을 적어 주세요.」 / 「에이전트 이름을 적어 주세요.」 / 「같은 이름의 태스크나 에이전트가 이미 있습니다.」(비서·검열 이름 포함) |
| T-4a | 만들기를 누르면 진행 과정을 스트리밍으로 보여 준다(D-6): ①요구사항 분석(필요한 역량 추출) ②팀 설계(승인된 역할 카탈로그에서 패턴 선택: supervisor + 멤버) ③spawning(선언 → 샌드박스에 `agents apply` → 시드). 단계마다 진행 중/완료/실패와 로그 한 줄씩. 시안은 대화상자를 바로 닫으므로 프런트에 진행 표시 화면이 추가로 필요하다. |
| T-5 | 만든 뒤: 대화 상대 목록에 새 에이전트(아이콘 generic, 이니셜, 색 5색 순환, 상태 running, 안건 0, 태그, 추천 질문 3개), 좌측 태스크 목록에 항목(제목 태스크명, 부제 desk), 「담당자 N명」 +1, 곧바로 `#chat/{id}` 로 이동. |
| T-6 | 태그 라우팅: 에이전트를 지정하지 않은 질문에 태그(또는 태스크명)가 들어가면 비서가 이 에이전트를 후보로 찾는다. |
| T-7 | 새 에이전트에게 물으면 「방금 만들어진 태스크라 아직 들어온 요청이 없습니다.」 류의 답. |
| T-8 | 추천 질문 기본값 3개: 「{태스크명} 관련해서 지금 들어온 요청 정리해줘」, 「{태그1} 관련 문의가 있었어?」(태그 없으면 「{태스크명}에서 결재가 필요한 안건 있어?」), 「이 에이전트가 맡은 안건 중 결재가 필요한 게 있어?」 |
| T-9 | desk: 에이전트 이름의 첫 영숫자 토큰 소문자 + `-desk`. 이니셜: 「에이전트」를 뗀 이름의 라틴 단어 앞글자 2개, 없으면 앞 2글자. |

---

## 3. API

| # | API | 상태 | 내용 |
|---|---|---|---|
| A-1 | `GET /me` | 확장 | `{authenticated, role, isAdmin, name}` |
| A-2 | `GET /agents` | 확장 | 대화 상대 목록(아래) |
| A-3 | `GET /tasks` | 확장 | 좌측 태스크 목록(아래) |
| A-4 | `POST /tasks` (SSE) | 신규 | 태스크 추가, 진행 과정 스트리밍 |
| A-5 | `GET /tasks/{id}` | 신규 | 생성 상태(스트림이 끊겼을 때 확인용) |
| A-6 | `GET /conversations?agentId=` | 신규 | 대화 내역 `[{id, agentId, title, preview, updatedAt, busy, count}]` |
| A-7 | `POST /conversations` | 신규 | `{agentId}` → 새 대화 |
| A-8 | `GET /conversations/{id}` | 신규 | 메시지 + 완성된 비서 턴(진행 과정 포함) |
| A-9 | `POST /chat` (SSE) | 변경 | `{text, role, conversationId, agentId?}` → §1.2 이벤트 |

A-2 항목:
```jsonc
{ "id": "research", "name": "리서치 에이전트", "kind": "assistant" | "task",
  "icon": "star|github|research|slack|mail|generic", "color": {"bg": "#d5eede", "fg": "#14532d"}, "initials": "JI",
  "description": "…", "taskId": "…", "taskName": "…", "desk": "…",
  "status": "running" | "waiting_decision" | "stopped" | "applying",
  "itemCount": 0, "tags": [], "suggestions": [] }
```
비서 항목의 `suggestions` = 전역 추천 질문 4개. 「담당자 N명」 = `kind=task` 개수.

A-3 항목: `{id, name, agentId, desk, icon, color, initials, itemCount, status: ready|applying|failed}`

A-4 `POST /tasks` (D-6):
- 요청 `{name, agentName?, description?, tags[]}`
- 스트림 시작 전 거절은 일반 JSON: `403 owner_only` / `409 name_conflict` / `422 task_name_required`(에이전트 이름은 비면 서버가 채움) / `503 tasks_unavailable` — `message` 는 §2 문구
- 통과하면 `200 text/event-stream`, 봉투는 `/chat` 과 같다:

| type | data |
|---|---|
| `task.start` | `{taskId, name, agentName}` |
| `task.stage` | `{stage: analyze\|design\|spawn, status: running\|done\|error, ms, detail}` |
| `task.log` | `{stage, text}` — 예 「필요 역량: 사내 근거 조회, 요약, 검증」「teams.yaml 선언」「rfa-main 에 agents apply」「시드 3건」 |
| `task.done` | `{task: TaskView, agent: AgentView}` |
| `task.error` | `{stage, code: no_role_for_requirement\|invalid_team\|apply_failed, message}` |

  `detail`: analyze → `{capabilities:[{id, label}]}`, design → `{roles:[…], supervisor, members:[{agentId, role}], sandbox}`, spawn → `{agentsApply: ok\|error, seeded}`.
- 동작: 검증 → 이름 충돌 검사 → `TeamService.create` 에 진행 hook 을 달아 analyze(패터너의 역량 추출)·design(역할 선택·compose)·spawn(선언·apply·시드)을 이벤트로 보냄 → `tasks` 테이블에 표시 메타 저장 → 태그를 head 키워드로 등록
- 연결이 끊겨도 생성은 계속된다. 프런트는 `GET /tasks/{id}` 로 `applying|ready|failed` 를 확인한다.

`itemCount`·참조 칩(`refs`)은 결재 데이터라 이번에는 0·빈 목록으로 둔다.

---

## 4. 결정 (2026-09-28 확정)

| # | 결정 |
|---|---|
| D-10 | task 1개 = 대화 상대 1개. 표시 메타는 SQLite `tasks` 로 덧씌움 |
| D-5 | 지정 대화는 head 없이 바로 위임 |
| D-12 | 상황에 따라 1명 또는 여러 명 동시. 담당자 탐색 점수가 threshold 이상인 후보 전부 병렬 위임 |
| D-13 | 비서가 직접 답하되 hallucination 금지: 모르는 사내 정보는 「알 수 없습니다」 |
| D-14 | 사외 질문의 진행 과정은 정해진 문구만, 원문은 사내만 |
| D-15 | `delegate.log` 는 서버 단계 로그로 시작 |
| D-7 | owner 대화만 SQLite 저장, guest 는 `history` |
| D-6 | `POST /tasks` 스트리밍: 요구사항 분석 → 팀 설계(패턴 선택) → spawning |

## 5. 개발 순서

1. A-1·A-2·A-3 응답 확장 + `tasks` 메타 테이블
2. A-6~A-8 대화 저장 (D-7)
3. A-9 `/chat` 이벤트 v2: 점수 탐색·threshold 선택·병렬 위임·합치기, 지정 대화, 직접 답변, 사외 trace 가림 (D-5, D-12, D-13, D-14, D-15)
4. A-4·A-5 태스크 추가 스트리밍 (D-6, D-10)

단위마다 `make mock-e2e` 와 API 스모크 결과를 보고한다.
