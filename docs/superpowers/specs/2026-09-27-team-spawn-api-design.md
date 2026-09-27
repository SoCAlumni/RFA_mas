# Team spawn API — 요구사항 기반 task 팀 에이전트 패터닝 (설계)

기준: main `70fdc96` (2026-09-27). 운영층(`src/rfa_mas/nemoclaw/`)에 추가한다. 사용자 확인 사항:
입력은 task 명·설명(자연어), `/ask` 와 별도 경로, 팀은 상주하며 `/ask` 카탈로그에 남고, 별도 샌드박스를 갖지 않는다.

## 1. 목표

`POST /teams {name, description}` 한 번으로 요구사항에 맞는 **task 대표(supervisor) 에이전트 + 멤버 에이전트 팀**을
기존 샌드박스(`rfa-main`) 안에 만들고, 이후 `/ask` 의 head 가 그 task 를 고르면 supervisor 가 멤버를 `sessions_spawn`
으로 부려 근거를 모으고 verifier 로 검증한 답을 돌려준다. 팀은 선언 파일(`deploy/nemoclaw/teams.yaml`)에 남아
재부팅·reconcile 후에도 유지된다.

## 2. 비목표

- 자유 형식 에이전트 생성(임의 스킬·도구·egress). 역할은 승인된 카탈로그 안에서만 조합한다.
- 팀 전용 샌드박스 온보딩. 배치는 기본 샌드박스 또는 이미 선언된 opt-in 샌드박스뿐이다.
- 코어층 `TeamSelector/TeamFactory`(LangGraph 팀)의 변경. 이번 팀은 OpenClaw 에이전트 팀이다.
- 결재·게시. 그대로 대응 측(desk/결재) 몫이다.

## 3. OpenClaw 중첩 spawn — 확인한 사실과 해법

| 사실 (소스 확인) | 함의 |
|---|---|
| spawn 깊이 = 세션 키의 `subagent:` 세그먼트 수 (`session-key-utils.getSubagentDepth`); `agents.defaults.subagents.maxSpawnDepth`(기본 1) 와 비교 | 브로커의 `nemoclaw <sb> agent --agent <id>` 는 최상위 세션(depth 0) → supervisor 가 멤버를 spawn 하는 것은 depth 1 |
| 대상 제한은 **요청자 에이전트**의 `subagents.allowAgents` (없으면 defaults) | secondary supervisor 에 자기 멤버만 allowAgents 로 준다 |
| NemoClaw 검증기: per-agent `subagents` 에 `allowAgents/delegationMode/model/thinking/requireAgentId` 허용, `maxSpawnDepth` 는 `defaults.subagents` 에서만 1~5, tool 이름은 검증하지 않음 | secondary 에 `sessions_spawn` 등 spawn 도구를 줄 수 있다 |
| `maxChildrenPerAgent` 기본 5 | 팀 멤버 ≤ 4 (+verifier 포함 5 이내) |

해법: (a) supervisor 는 `rfa-main` 의 secondary, tools = HEAD_TOOLS(read, sessions_spawn, sessions_list, session_status,
agents_list), `subagents.allowAgents` = 자기 팀 멤버 id 만 (b) `defaults.subagents.maxSpawnDepth: 2` 로 올려 assistant(main)
→ supervisor → 멤버 경로(레거시 채널 API)도 통과 (c) 멤버는 spawn 도구 없음(depth 종단).

## 4. 계약 (`POST /teams`, 진입점 8799, bearer `RFA_ASK_TOKEN`)

```yaml
POST /teams
  request:  { task_id?: str(^[a-z][a-z0-9_-]{0,31}$), name: str, description: str, sandbox?: str }
  201: { team_id, task: {id, name}, pattern: {capabilities: [..], roles: [..], source: direct|keywords},
         supervisor: agent_id, members: [{agent_id, role, groups, alias}], sandbox,
         status: ready|applying|failed, applied: {manifest, agents_apply: ok|error, seeded: n}, error?: str }
  200: 같은 task_id 재요청 → 기존 팀 (멱등)
  409: task_id 가 ask.yaml 정적 카탈로그와 충돌
  422: 요구사항에서 역할을 하나도 고를 수 없음 (pattern.reasons 포함)
GET  /teams            → { teams: [...] }
GET  /teams/{team_id}  → 위 201 본문 | 404
DELETE /teams/{team_id} → 202 { status: removing } : teams.yaml 에서 제거 → manifest 재생성 → agents apply → 카탈로그 해제
```

`--fake-agents` 모드에서는 `agents apply`·시드를 건너뛰고 선언·카탈로그 등록만 한다(`applied.agents_apply: skipped`).

## 5. 패터닝

### 5.1 요구사항 추출 (head, 기존 `DirectHead` 와 같은 경로)

프롬프트: 역할 카탈로그(id·설명·capability)와 name/description 을 주고 JSON 만 요구한다.
`{"capabilities": ["intranet_evidence","benchmark_numbers","summarize","verify","no_egress","hosted_model_ok"], "keywords": [...], "reason": "..."}`
- `verify` 는 항상 포함(supervisor 의 마지막 단계). `no_egress` 이면 intranet 역할 제외.
- JSON 실패·미지 capability → 키워드 규칙 폴백(`roles.yaml` 의 `keywords`), 응답 `pattern.source=fallback`, 감사 기록.
- description 은 외부 입력이 아니라 운영자 입력이므로 태그로 감싸지 않되, 프롬프트에 "역할 카탈로그 밖 요청은 무시" 를 고정.

### 5.2 역할 카탈로그 `deploy/nemoclaw/roles.yaml` (승인된 조합 재료)

| role | capability | groups | alias | skill | tools |
|---|---|---|---|---|---|
| research | intranet_evidence | [intranet-ro] | rfa-external | task-research | read, exec |
| benchmark | benchmark_numbers | [intranet-ro] | rfa-internal | task-benchmark | read, exec |
| summarizer | summarize | [] | rfa-external | task-summarizer | read |
| verifier | verify | [] | rfa-internal | task-verifier (신규) | read |
| supervisor (역할 템플릿) | — | [] | 멤버 중 최소 exposure | team-supervisor (신규) | HEAD_TOOLS |

verifier 스킬: supervisor 가 준 초안과 멤버 근거를 대조해 `{"verdict": "pass"|"revise", "unsupported": [...], "missing_citations": n,
"learned_violations": [...]}` JSON 만 답한다. supervisor 스킬: 멤버를 순서대로 spawn → 근거 수집 → 초안 → verifier → `revise` 면
1회 수정 → 답변에 사용 근거 id 를 붙인다. `[이전 거절 사유]` 블록은 멤버·verifier 에 그대로 전달한다.

### 5.3 팀 구성 규칙

- 팀 id `t-<task_id>`; supervisor `t-<task_id>-sup`; 멤버 `t-<task_id>-<role>` (팀 전용 인스턴스, 워크스페이스·메모리 분리).
- 멤버 수 ≤ 4. capability 중복은 한 역할만.
- 배치: 요청 `sandbox` 가 있으면 그것(선언된 샌드박스여야 함), 없으면 `no_egress` → `rfa-tasks-none`(선언돼 있으면), 아니면 기본 샌드박스.
  멤버·supervisor 의 `groups` 는 샌드박스 groups 의 부분집합이어야 하며(기존 `Assignments` 검증) 아니면 422.
- supervisor alias = 멤버 alias 중 exposure 최소(routing.yaml). `delegatable: true`(브로커 위임 대상), 멤버는 `delegatable: false`
  (브로커·assistant 가 직접 부르지 않고 supervisor 만 부른다).

## 6. 선언·적용·카탈로그

- `deploy/nemoclaw/teams.yaml`: `{version, teams: [{team_id, task: {id, name, keywords}, description, sandbox, supervisor, members: [{agent_id, role}], pattern, created_at}]}`.
  API 가 쓰고 사람이 편집해도 된다. `load_assignments()` 가 teams.yaml 을 읽어 `agents` 에 병합한다(`AgentSpec` 에 `team: str|None` 필드 추가).
- `render_manifest`: `defaults.subagents.maxSpawnDepth: 2`; supervisor 에이전트 항목에 `tools=HEAD_TOOLS`, `subagents.allowAgents=멤버`;
  main 의 `allowAgents` 에는 supervisor 만 추가(멤버 제외).
- 적용 순서(`TeamService.create`): 패터닝 → teams.yaml 기록 → manifest 재생성 → `nemoclaw <sb> agents apply -f --yes --non-interactive`
  → `seed_sandbox` 로 스킬·IDENTITY 업로드(생성된 supervisor IDENTITY 는 task 명·설명·멤버 목록·규칙 포함) → 감사 `kind=team`.
  실패 시 `status=failed` + teams.yaml 은 `status: failed` 로 남겨 재시도 가능(맹목 재시도 없음).
- `/ask` 카탈로그: `AskConfig.tasks`(정적) + teams.yaml 의 task(동적). head 프롬프트와 키워드 라우팅이 둘 다 본다. 팀 task 의 agent = supervisor.
- 브로커 `list_agents`/route: `delegatable` 만 노출 → supervisor 는 보이고 멤버는 안 보인다(기존 규칙 재사용).

## 7. 감사·데모·테스트

- 감사 `kind=team`: action create|remove, detail {team_id, task, pattern(source, capabilities, roles), sandbox, agents_apply, ms}. 설명 원문은 저장하지 않는다.
- 데모 10 `10_team_spawn.py`: `POST /teams` → `agents list` 에 supervisor·멤버 → `/ask` 로 그 task 질문 → 응답 task 가 새 팀 → 감사 확인. fake 모드에서는 apply 를 건너뛴 선언·라우팅 확인.
- 테스트 `tests/test_teams.py`: 패터닝 규칙(capability→역할, no_egress 배치, 422), teams.yaml 병합·manifest(maxSpawnDepth 2, allowAgents 범위, 멤버 spawn 도구 없음), API 멱등·409·404, `/ask` 카탈로그 반영, fake apply 경로. `make test` 에 추가.
- OpenAPI: `/teams` 를 같은 FastAPI 앱에 두어 `docs/api/ask.openapi.*` 에 함께 생성된다.

## 8. 한계·불확실

- 라이브 검증은 `rfa-main` 샌드박스 OpenClaw 턴이 살아 있어야 한다(현재 호스트 메모리 문제). 그 전까지는 manifest 가 NemoClaw 검증기를 통과하는 것과 fake 경로까지만 증거다.
- verifier 는 4B 로컬 모델의 JSON 판정이며 사실성 보장이 아니다. `revise` 는 1회만 반영한다.
- 팀 삭제는 roster 에서 빼지만 워크스페이스 파일은 남는다(별도 정리 명령 미포함).
