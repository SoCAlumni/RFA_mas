# 프런트 API 연동 가이드 (UI 개발용)

UI 화면마다 어떤 API 를 언제 부르고, 응답을 어디에 그리는지 정리한 문서다. 필드 정의 원본은 `docs/openapi.yaml`(서버에서 `GET /openapi.json`, `/docs`)이고, 이 문서는 **화면 ↔ API 대응과 주의점**을 다룬다.

- 기준 시안: artifact `LFD3Utajj2ahC9wv45MbAg` (비서 채팅 · 태스크 추가 · 관리 화면)
- 배경 결정: `docs/decisions.md` (D-0.4, D-5~D-7, D-10, D-12~D-19), 흐름·보안: `docs/ARCHITECTURE.md`
- 작성: 2026-09-28, FE-5·FE-6 (커밋 전 작업 트리 기준)

---

## 0. 공통 규칙

### 0.1 서버·연결

| 항목 | 값 |
|---|---|
| Base URL | `http://127.0.0.1:8799` (`make serve` 의 entry, loopback 전용) |
| CORS | **없음**. 개발 서버 프록시로 같은 origin 처럼 붙인다 (예: Vite `server.proxy` 에 `/me`, `/agents`, `/tasks`, `/conversations`, `/chat`, `/admin` → `http://127.0.0.1:8799`) |
| 인증 | `Authorization: Bearer <RFA_ASK_TOKEN>` (`.env.dev`). 토큰이 맞으면 owner(관리자), 없거나 틀리면 guest. **401 은 없다**: 조회는 guest 도 200, 쓰기는 403 `owner_only` |
| 문자 | 모든 `message`·`note`·힌트 문구는 한국어 완성문. **그대로 화면에 표시**한다 |

### 0.2 오류 본문

```jsonc
{ "code": "name_conflict", "message": "같은 이름의 태스크나 에이전트가 이미 있습니다." }
```

- `code` 로 분기하고 `message` 를 그대로 보여 준다.
- ⚠️ 요청 **형식 오류**(글자 수 초과, 타입 틀림)는 FastAPI 기본 형식 `{"detail": [{loc, msg, type}]}` 로 온다(422). 클라이언트 검증으로 먼저 막고, 오류 처리기는 `body.message ?? body.detail?.[0]?.msg` 로 둘 다 받는다.

### 0.3 SSE (`POST /chat`, `POST /tasks`)

POST 스트림이라 `EventSource` 를 못 쓴다. `fetch` + `ReadableStream` 으로 읽는다. 모든 프레임은 같은 봉투를 쓴다.

```
id: 3
event: agents.search
data: {"type":"agents.search","runId":"run-1a2b3c4d5e6f","seq":3,"ts":1790530000.123,"data":{...}}
```

| 필드 | 의미 |
|---|---|
| `type` | 이벤트 이름 (`event:` 줄과 같음) |
| `runId` | 실행 id. 채팅은 `run-…`, 태스크 추가는 `task-{taskId}` |
| `seq` | 0부터 1씩 증가 |
| `ts` | unix 초 |
| `data` | 이벤트별 본문 (아래 표) |

**중지** = 클라이언트가 `AbortController.abort()` 로 연결을 끊는다. 중지용 별도 API 는 없다.

```ts
export async function* sse(url: string, body: unknown, token?: string, signal?: AbortSignal) {
  const res = await fetch(url, {
    method: "POST", signal,
    headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.headers.get("content-type")?.includes("text/event-stream")) {
    throw await res.json();                       // {code, message} 또는 {detail}
  }
  const reader = res.body!.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += value;
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i); buf = buf.slice(i + 2);
      const line = frame.split("\n").find(l => l.startsWith("data: "));
      if (line) yield JSON.parse(line.slice(6)) as { type: string; runId: string; seq: number; ts: number; data: any };
    }
  }
}
```

시안의 목 함수 `runQuery(req, signal) → AsyncIterable<Event>` 를 위 `sse("/chat", …)` 로 바꾸면 리듀서는 거의 그대로 쓸 수 있다(§2.3).

### 0.4 등급(사내/사외)

- 등급은 **요청마다** 매긴다. 태스크·에이전트 응답에는 등급 필드가 없다(D-0.4).
- 채팅은 묻는 사람의 역할이 등급을 정한다: owner → 사내(`level: "company"`), guest → 사외(`level: "public"`).
- `/chat` 본문의 `role` 은 희망값일 뿐이다. 토큰 없이 `"owner"` 를 보내도 서버가 guest 로 낮춘다. `run.start.data.role` / `level` 을 **서버 값으로 다시 반영**한다.

---

## 1. 앱 시작 · 사이드바

| 시점 | 호출 | 쓰는 곳 |
|---|---|---|
| 앱 로드 | `GET /me` | 우상단 이름(`name`), 관리자 메뉴 노출(`isAdmin`), 쓰기 버튼 활성화(`role === "owner"`) |
| 앱 로드 | `GET /tasks` | 좌측 태스크 목록 |
| 앱 로드 | `GET /agents` | 대화 상대 목록, 「담당자 N명」 |
| 관리 메뉴 보일 때 | `GET /admin/agents` → `counts.total` | 사이드바 「에이전트 N」 |
| 관리 메뉴 보일 때 | `GET /admin/sandboxes` → `sandboxes.length` | 사이드바 「샌드박스 N」 |

### `GET /me`

```jsonc
{ "authenticated": true, "role": "owner", "isAdmin": true, "name": "소유자" }
// guest: { "authenticated": false, "role": "guest", "isAdmin": false, "name": "게스트" }
```

### `GET /tasks` → `TaskView[]` (좌측 태스크 목록)

| 필드 | UI |
|---|---|
| `name` | 항목 제목 |
| `desk` | 항목 부제 |
| `icon`, `color{bg,fg}`, `initials` | 아이콘 타일 (`icon === "generic"` 이면 `initials` 표시) |
| `itemCount` | 결재 대기 배지 (지금은 결재 연동 전이라 0) |
| `status` | `ready` 일반 / `applying` 「만드는 중」 스피너 / `failed` 는 목록에 안 나옴 |
| `agentId` | 말풍선 버튼 → `#chat/{agentId}` |
| `worker`, `sandbox`, `securityLabel`, `error`, `source` | 관리자용 정보. 일반 화면에는 쓰지 않는다 |

### `GET /agents` → `AgentView[]` (대화 상대)

비서 1개 + 태스크마다 에이전트 1개. 검열 에이전트는 없다.

| 필드 | UI |
|---|---|
| `kind` | `assistant` / `task` — 「담당자 N명」 = `kind === "task"` 개수 |
| `name`, `description` | 카드 이름·설명 |
| `icon`, `color`, `initials` | 아이콘 |
| `status` | 상태 점. `running` 초록, `applying` 은 「만드는 중」 |
| `taskName` | 부제 「{상태} · {taskName}」 (비서는 「담당자 N명을 관장」) |
| `tags` | 지정 대화 빈 화면의 `#태그` |
| `suggestions` | 빈 화면 추천 질문 (비서 = 전역 4개, 에이전트 = 자기 것). 누르면 바로 전송 |
| `itemCount` | 「맡은 안건 N건」 (지금은 0) |

---

## 2. 「검색하거나 물어보기」 (비서 채팅)

### 2.1 화면별 호출

| 화면·동작 | 호출 |
|---|---|
| `#chat` 진입 (비서) | `GET /conversations?agentId=assistant` → 마지막으로 연 대화 → 가장 최근 대화 → 없으면 새 대화 |
| `#chat/{agentId}` 진입 (지정 에이전트) | `GET /conversations?agentId={agentId}` 로 같은 순서 |
| 「대화 내역」 목록 | `GET /conversations?agentId=…` (최신순, `title` = 첫 질문) |
| 「바꾸기」 메뉴 | 에이전트마다 `GET /conversations?agentId=…` 의 첫 항목: `busy ? "답변 중…" : title`, `updatedAt` |
| 「+ 새 대화」 | `POST /conversations {agentId}` → `id` 를 이후 `/chat` 의 `conversationId` 로 |
| 대화 다시 열기 | `GET /conversations/{id}` → `messages` 를 그대로 렌더 |
| 질문 전송 | `POST /chat` (SSE) |
| 「중지」 | 스트림 `abort()` |

**owner 와 guest 의 차이 (D-7)**

| | owner | guest |
|---|---|---|
| `GET /conversations` | 저장된 목록 | 항상 `[]` |
| `POST /conversations` | 저장, `persisted: true` | id 만 발급, `persisted: false` |
| `GET /conversations/{id}` | 메시지 | 404 |
| 이전 턴 | 서버가 가진다 (`history` 불필요) | **클라이언트가 들고 있다가** `history` 로 보낸다 |

### 2.2 `POST /chat`

```jsonc
{
  "text": "NPU SDK 1.9 변경점 알려줘",      // 1..10000자
  "role": "owner",                          // 희망값, 서버가 토큰으로 확정
  "conversationId": "c_1a2b3c4d5e6f",       // [A-Za-z0-9_.:-]{1,64}. 없어도 되지만 owner 저장·중복 방지에 필요
  "agentId": "npu",                         // 지정 대화일 때만. 비서면 생략 또는 "assistant"
  "history": [ { "role": "user", "text": "…" }, { "role": "assistant", "text": "…" } ]  // guest 만, ≤50개, 각 ≤8000자
}
```

스트림 전 거절:

| 상태 | code | UI |
|---|---|---|
| 409 | `conversation_busy` | 「이 대화에서 아직 답변 중입니다.」 한 대화에는 한 질문만. 입력창은 실행 중 「중지」로 바뀌어야 한다 |
| 422 | (`detail`) | 형식 오류 |

**다른 대화는 동시에 여러 개 답변을 받을 수 있다.** 대화마다 `AbortController` 를 따로 둔다.

### 2.3 채팅 이벤트 → 답변 카드

순서: `run.start → assistant.delta → agents.search → agents.select → (delegate.start → delegate.log* → delegate.end)×N → guard.start → guard.end → answer.delta* → run.end`

| type | data | 그리는 곳 |
|---|---|---|
| `run.start` | `{runId, conversationId, messageId, role, agentId, level, requestId}` | 비서 턴 생성(`messageId`). `level` 로 「사내/사외」 표시, `role` 로 guest 화면 전환 |
| `assistant.delta` | `{text}` | ① 요청 파악: 서두 한 줄 (「NPU SDK 에이전트에게 확인하겠습니다.」) |
| `agents.search` | `{query, candidates: [{agentId, reason, score}]}` | ② 담당자 탐색: 후보 목록 + 이유 + 점수(0..1). 빈 배열이면 「맞는 담당자가 없습니다. 비서가 직접 답합니다.」 |
| `agents.select` | `{selected: [{agentId, task}], skipped: [{agentId, reason}]}` | 선택/제외 표시. `selected` 가 비면 ③ 단계는 「해당 없음」 |
| `delegate.start` | `{callId, agentId, task}` | ③ 담당자 호출: `callId` 별 행 추가, 상태 「응답 기다리는 중」, 펼치면 `task`(위임 내용) |
| `delegate.log` | `{callId, text}` | 그 호출의 로그 줄 추가 |
| `delegate.end` | `{callId, status, summary, refs, durationMs}` | 상태 확정 + 「1.4초」 |
| `guard.start` | `{level}` | ④ 등급 검사: 「{사외/사내} 등급으로 검사 중」 |
| `guard.end` | `{status, note, redactions}` | 통과 / 「N건 제외」 / 차단 + `note` |
| `answer.delta` | `{text}` | 답변 본문에 이어 붙이기 (문장 단위로 온다) |
| `run.end` | `{status, durationMs, messageId, error?, code?}` | 꼬리 문구 (아래) |

`delegate.end.status` → 표시:

| status | 표시 |
|---|---|
| `ok` | 답 받음 |
| `none` | 해당 없음 (근거 없음) |
| `blocked` | 샌드박스 정책으로 차단됨 (예약값, 지금은 안 나옴) |
| `error` | 실패 |

`guard.end.status`: `pass` / `redacted` / `blocked`. `level`: `company` = 사내, `public` = 사외.

**꼬리 문구 (`run.end`)**

| 상황 | 표시 |
|---|---|
| `status: "done"` | 「{durationMs/1000}초 · {사내/사외} 등급 답변」 + 참조 칩(`refs`, 지금은 빈 배열) |
| `status: "error"` | 「답변하지 못했습니다: {error} · 다시 시도」 |
| 사용자가 abort | 서버 이벤트 없음. 클라이언트가 「중지했습니다 · 다시 물어보기」로 마감 (서버는 stopped 턴으로 저장) |

`run.end.code` 예: `unknown_agent`(없는 담당자), `agent_not_ready`(만드는 중인 태스크에 지정 대화), `chat_failed`(내부 오류).

**guest 화면 (D-14)**: `delegate.log`·`delegate.end.summary` 는 정해진 문구만 온다(「담당 에이전트에 위임」「응답 수신 · N자」). 서버가 가려서 보내므로 UI 에서 따로 가릴 필요는 없다.

### 2.4 저장된 대화 (`GET /conversations/{id}`)

```jsonc
{
  "id": "c_…", "agentId": "assistant", "title": "NPU SDK 1.9 변경점 알려줘", "preview": "…",
  "createdAt": 1790530000.1, "updatedAt": 1790530012.4, "busy": false, "count": 1, "persisted": true,
  "messages": [
    { "id": "u12", "role": "user", "text": "…", "ts": 1790530000.2 },
    { "id": "a_1a2b…", "role": "assistant", "ts": 1790530012.4,
      "status": "done|stopped|error", "phase": "idle", "preface": "…",
      "search": {"query", "candidates"}, "selection": {"selected", "skipped"},
      "calls": [{"callId","agentId","task","status","logs":[],"refs":[],"summary","durationMs"}],
      "guard": {"level","status","note","redactions"}, "answer": "…",
      "runId": "run-…", "durationMs": 3912, "error": null }
  ]
}
```

비서 턴의 모양은 **SSE 리듀서가 만드는 턴과 같다**(서버의 `TurnBuilder` 가 시안 리듀서를 그대로 따른다). 라이브 턴과 다시 연 턴을 같은 컴포넌트로 그린다.

`busy: true` 인 대화를 다시 열면 아직 답변 중이다. 그 스트림에 다시 붙는 API 는 없으니, 「답변 중…」을 보여 주고 잠시 뒤 다시 조회한다.

---

## 3. 「태스크 추가」

owner 전용. guest 에게는 버튼을 비활성화하고 「소유자만 할 수 있어요」를 보여 준다(서버도 403).

### 3.1 폼 → 요청

| 필드 | 제약 | 비었을 때 |
|---|---|---|
| `name` 태스크명 | 필수, ≤30자 | 422 `task_name_required` 「태스크명을 적어 주세요.」 |
| `agentName` 에이전트 이름 | ≤40자 | 서버가 「{태스크명} 대응 에이전트」 |
| `description` 설명 | ≤200자 | 서버가 「{태스크명} 태스크를 맡은 에이전트입니다.」 |
| `tags` | 서버가 `#` 제거·중복 제거 후 8개까지 | — |

안내 문구: 「태스크와 에이전트는 1:1로 만들어집니다. 사내·사외 등급은 태스크가 아니라 들어오는 결재 대상마다 매겨집니다.」 **등급 선택 UI 는 없다.**

글자 수 초과는 422 `detail` 형식으로 오므로(§0.2) **폼에서 먼저 막는다**.

### 3.2 `POST /tasks` 응답

스트림 전 거절 (JSON):

| 상태 | code | message |
|---|---|---|
| 403 | `owner_only` | 게스트는 태스크와 에이전트를 추가할 수 없습니다. 소유자에게 요청하세요. |
| 409 | `name_conflict` | 같은 이름의 태스크나 에이전트가 이미 있습니다. (비서·검열 이름 포함) |
| 422 | `task_name_required` | 태스크명을 적어 주세요. |
| 503 | `tasks_unavailable` | 지금은 태스크를 추가할 수 없습니다. |

통과하면 `200 text/event-stream`. **대화상자를 닫고 진행 화면(3단계)을 보여 준다**(시안에는 없는 화면이라 새로 만든다).

| type | data | UI |
|---|---|---|
| `task.start` | `{taskId, name, agentName}` | 진행 화면 제목. `taskId` 를 저장해 둔다 (끊겼을 때 조회용) |
| `task.stage` | `{stage, status, ms, detail}` | 단계 표시 `analyze` 요구사항 분석 / `design` 팀 설계 / `spawn` 에이전트 생성. `status`: `running` / `done` / `error` |
| `task.log` | `{stage, text}` | 그 단계 아래 로그 한 줄 |
| `task.done` | `{task: TaskView, agent: AgentView, ms}` | 좌측 목록·대화 상대 목록에 추가, 「담당자 N명」 +1, `#chat/{agent.id}` 로 이동 |
| `task.error` | `{stage, code, message, detail}` | 그 단계에 실패 표시 + `message` |

`task.stage.detail`:
- `analyze` → `{capabilities: [{id, label}], source}` (필요 역량 칩)
- `design` → `{roles, supervisor, members: [{agentId, role}], sandbox}` (팀 구성도)
- `spawn` → `{agentsApply: "ok"|"error", seeded}`

`task.error.code`: `no_role_for_requirement`, `invalid_team`, `apply_failed`, `task_create_failed`.

spawn 은 최대 300초 걸린다. 진행 화면에 경과 시간을 보여 준다.

### 3.3 연결이 끊겼을 때

생성은 서버에서 계속된다. `GET /tasks/{taskId}` 로 상태를 확인한다.

| `status` | 처리 |
|---|---|
| `applying` | 5초마다 다시 조회 |
| `ready` | 완료. `GET /agents`, `GET /tasks` 새로고침 |
| `failed` | `error` 표시 (이 API 는 failed 도 돌려준다. 목록 `GET /tasks` 에는 안 나온다) |

⚠️ 서버가 생성 중에 재시작되면 `applying` 이 풀리지 않는다(알려진 문제 §6). 폴링에 **상한(예: 6분)**을 두고, 넘으면 「생성 상태를 확인할 수 없습니다」로 멈춘다.

---

## 4. 관리 · 「에이전트」 (`/admin/agents`)

조회는 누구나, 쓰기는 owner 만. 관리 에이전트(`assistant`, `censor`)는 owner 도 바꿀 수 없다.

### 4.1 목록 `GET /admin/agents`

```jsonc
{ "agents": [AdminAgent…], "counts": {"total": 21, "task": 19, "management": 2}, "observedAt": 1790530000.0, "hint": "…" }
```

이미 정렬되어 온다(태스크 담당 → 팀 supervisor 와 그 멤버 → 나머지 → 관리). **클라이언트에서 다시 정렬하지 않는다.**

| 필드 | UI |
|---|---|
| `icon`/`color`/`initials` | 이니셜 타일 |
| `name` | 1줄 (에이전트 id) |
| `subtitle` | 2줄 「{태스크} 태스크」 / 「{supervisor} 팀 멤버 · 역할」 / 관리 역할 |
| `sandboxLine` | 3줄 「rfa-main 샌드박스」 |
| `status` | 상태 pill `running` / `stopped` / `applying` |
| `observed` | `false` 면 상태가 선언값이다. 흐린 pill 또는 「확인 전」 |
| `callsToday` | 「오늘 N회」 |
| `group`, `role`, `parentId` | 그룹 구분선, 멤버 들여쓰기 |
| `editable`, `readOnlyReason` | `false` 면 모든 쓰기 버튼 비활성화 + `readOnlyReason` 힌트 (「데모에서는 관리 에이전트를 수정할 수 없습니다.」) |

「추가」 버튼 → §3 태스크 추가 대화상자.

상태는 서버가 60초마다 갱신한다. 목록 폴링은 30~60초면 충분하다.

### 4.2 상세 `GET /admin/agents/{id}`

| 영역 | 필드 |
|---|---|
| 헤더 | `name`, `description`, `status`, `sandbox` → 「{sandbox} 샌드박스 설정 보기」 링크 (`#admin/sandboxes/{sandbox}`) |
| 컨텍스트 | `context` (없으면 `null` → 「아직 LLM 호출 기록이 없습니다」). 「지금 쓰는 양 {usedTokens} / {limitTokens} 토큰」, 막대 = `breakdown.systemPrompt / toolDefinitions / memoryNotes / conversation` + 남은 공간(`limitTokens - usedTokens`). `measured: true` 면 합계가 실측이고 부분은 추정 |
| 불러오는 자료 | `sources[]` 마다 스위치. `available: false` 면 비활성화 + `unavailableReason` |
| 호출 통계 | `stats`: 「오늘 · {provider} · {model}」, `calls`, `tokensThousands`(천), `avgLatencySeconds`, `blockedCalls`, `last7Days[{day, calls}]` 막대 |
| 버튼 활성화 | `actions.{compact, clearMemory, editPrompt, toggleSources}` (owner 가 아니거나 관리 에이전트면 false) |

### 4.3 쓰기 (owner)

| 버튼 | 호출 | 응답 → UI |
|---|---|---|
| 「대화 압축」 | `POST /admin/agents/{id}/compact` | `{applied, removedSessions, note}` → `note` 토스트, 상세 새로고침 |
| 「기억 비우기」 | `POST /admin/agents/{id}/clear-memory` | 같음. **확인 대화상자를 먼저 띄운다** (세션·MEMORY.md 삭제, 되돌릴 수 없음) |
| 「시스템 프롬프트 편집」 열기 | `GET /admin/agents/{id}/prompt` | `identity`(읽기 전용, 서명 마커 가림), `skill`/`skillText`(읽기 전용), `instructions`(편집 가능) |
| 저장 | `PUT /admin/agents/{id}/prompt {instructions}` (≤4000자) | `{applied, note, …PromptView}` → 「다음 대화부터 적용」 |
| 자료 스위치 | `PATCH /admin/agents/{id}/sources/{sourceId} {enabled}` | `{sources, applied, note}` → 스위치 목록 교체 |

이 요청들은 샌드박스 명령을 실행하므로 **수 초~수십 초** 걸린다. 버튼에 로딩을 걸고 중복 클릭을 막는다.

오류 code: `owner_only`(403), `management_agent`(403), `unknown_agent`/`unknown_source`/`unknown_action`(404), `source_unavailable`(409, 사내 지식 경로 없음), `sandbox_failed`(502, 샌드박스 명령 실패). 모두 `message` 를 그대로 표시.

---

## 5. 관리 · 「샌드박스」 (`/admin/sandboxes`)

### 5.1 목록 `GET /admin/sandboxes`

```jsonc
{ "sandboxes": [SandboxItem…], "limit": 2, "canAdd": false, "limitMessage": "샌드박스는 많은 양의 메모리를 요구합니다. 현재 데모에서는 2개까지만 제공드립니다.", "observedAt": … }
```

| 필드 | UI |
|---|---|
| `name`, `default` | 이름, 기본 배지 |
| `securityLabel` | 보안 레벨 「L2 control-plane+intranet-ro」 |
| `tasks[{id,name}]` | 연결된 태스크 |
| `provider`, `gatewayPort` | 「{provider} · 게이트웨이 {port}」 |
| `status`, `observed` | 상태 (`unknown` 가능) |
| `agentCount` | 「에이전트 N」 |

「추가」: `canAdd === false` 면 `POST` 없이 `limitMessage` 를 보여 준다. (`POST /admin/sandboxes` 는 항상 409 `sandbox_limit` 또는 501 `not_supported`)

### 5.2 상세 `GET /admin/sandboxes/{id}`

| 영역 | 필드 |
|---|---|
| 헤더 | `securityGroups[{id, privilege, description, presets, mcpServers}]`, `tasks`, `agentsManifest`, 「이 샌드박스의 에이전트 N개 보기」(`agents[]` → 에이전트 목록 필터) |
| LLM 추론 | `inference.providerOptions[]` 라디오: `selectable: false` 면 비활성화 + `reason` (Ollama 로컬 「로컬 LLM을 확인할 수 없습니다.」). 선택한 옵션의 `models[]` 로 모델 드롭다운(빈 배열이면 자유 입력). `contextLength` ∈ `contextLengthChoices`, `maxOutputTokens` ∈ `maxOutputChoices` 드롭다운. `pendingRecreate` 가 있으면 「다시 만들 때 적용」 배지 |
| 게이트웨이 | `gateway.port`, `registeredProvider`(`llm-api`), `hasKey`, `currentRoute`(「지금 추론 경로 …」), `sharedWith`(같은 경로를 쓰는 다른 샌드박스) |
| 안내 | `applyNote` |

### 5.3 「변경 적용」 `PATCH /admin/sandboxes/{id}`

**바뀐 필드만** 보낸다.

```jsonc
{ "provider": "gemini", "model": "gemini-3.5-flash-lite", "contextLength": 32768, "maxOutputTokens": 4096 }
```

응답 `{applied: ["provider","model"], requiresRecreate: ["contextLength"], sandbox: SandboxDetail}` → `sandbox` 로 화면을 통째로 교체하고, `applied` 는 「바로 적용됨」, `requiresRecreate` 는 「다시 만들 때 적용」으로 알린다.

- 제공자·모델은 **모든 샌드박스에 한꺼번에** 적용된다(프록시가 하나). 확인 대화상자에 `gateway.sharedWith` 를 보여 준다.
- ⚠️ 제공자와 길이를 한 요청에 같이 보내면, 길이가 잘못됐을 때 422 가 나도 **제공자는 이미 바뀌어 있다**(알려진 문제 §6). 길이는 드롭다운 선택지로만 받고, 422 뒤에는 반드시 상세를 다시 조회한다.

오류 code: `owner_only`(403), `unknown_sandbox`(404), `not_selectable`(409), `invalid_context_length`/`invalid_max_output`(422), `llm_unavailable`(503).

---

## 6. 알려진 문제 (UI 가 방어할 것)

2026-09-28 리뷰에서 나온 서버 쪽 문제 중 화면에 드러나는 것들이다. 서버가 고쳐지면 이 절을 갱신한다.

| # | 문제 | UI 방어 |
|---|---|---|
| K-1 | 형식 오류 422 가 `{detail:[…]}` 로 온다 (`{code,message}` 아님) | §0.2 처럼 두 형식 모두 처리, 폼 검증으로 먼저 막기 |
| K-2 | 생성 중 서버가 재시작되면 태스크가 영원히 `applying`, 같은 이름은 계속 409 | 폴링 상한, 「생성 상태를 확인할 수 없습니다」 |
| K-3 | spawn 단계(최대 300초) 동안 새 에이전트를 지정해 물으면 「만드는 중」 대신 위임 실패(`delegate.end error`) | `status === "applying"` 인 에이전트는 채팅 입력을 막고 「만드는 중입니다」 표시 |
| K-4 | 태스크 두 개를 동시에 만들면 한쪽 에이전트가 빠질 수 있다 | 진행 중인 생성이 있으면 「태스크 추가」 버튼 비활성화 |
| K-5 | `PATCH /admin/sandboxes` 가 422 여도 제공자는 이미 바뀌었을 수 있다 | 오류 뒤 상세 재조회 |
| K-6 | 채팅을 중지해도 서버의 위임 작업은 최대 2분 더 돈다 | 같은 대화에 곧바로 다시 물으면 정상 동작하지만 답이 느릴 수 있다. 중지 직후 「다시 물어보기」는 허용 |
| K-7 | `GET /admin/agents/{id}/prompt` 와 `GET /tasks` 의 `error`·`securityLabel` 이 guest 에게도 보인다 | guest 에게는 관리 메뉴와 프롬프트 보기 버튼을 숨긴다 (서버 권한 조정은 결정 대기) |
| K-8 | `pendingRecreate` 가 한 번 생기면 사라지지 않는다 | 배지를 「저장된 설정」 정도로만 표시 |

---

## 7. 이번 범위 밖

- **결재함·알림함·결재 상세, 태스크 소스 연결** (`/inbox*`, `/tasks/{id}/sources*`): 다른 세션에서 개발 중이다. 계약은 `docs/FE_APPROVAL_API_PLAN.md` 와 openapi 의 해당 경로를 보되, 바뀔 수 있다.
- `/ask`, `/v1/head/ask`, `/teams`, `/chat/sync`: desk·RFA_module·CLI 용이다. 프런트는 쓰지 않는다.

---

## 부록. 호출 한눈에 보기

| API | 인증 | 화면 |
|---|---|---|
| `GET /me` | 선택 | 앱 시작 |
| `GET /agents` | — | 대화 상대 목록 |
| `GET /tasks`, `GET /tasks/{id}` | — | 좌측 태스크 목록, 생성 상태 확인 |
| `POST /tasks` (SSE) | owner | 태스크 추가 |
| `GET /conversations?agentId=` | 선택 (guest = `[]`) | 대화 내역, 바꾸기 메뉴 |
| `POST /conversations` | 선택 | 새 대화 |
| `GET /conversations/{id}` | owner | 대화 다시 열기 |
| `POST /chat` (SSE) | 선택 (등급 결정) | 질문 |
| `GET /admin/agents`, `GET /admin/agents/{id}` | — | 에이전트 관리 목록·상세 |
| `POST /admin/agents/{id}/compact` · `/clear-memory` | owner | 대화 압축, 기억 비우기 |
| `GET` · `PUT /admin/agents/{id}/prompt` | 조회 — / 저장 owner | 시스템 프롬프트 편집 |
| `PATCH /admin/agents/{id}/sources/{sourceId}` | owner | 불러오는 자료 스위치 |
| `GET /admin/sandboxes`, `GET /admin/sandboxes/{id}` | — | 샌드박스 목록·상세 |
| `PATCH /admin/sandboxes/{id}` | owner | 변경 적용 |
| `POST /admin/sandboxes` | owner | 추가 (항상 한도 안내) |
