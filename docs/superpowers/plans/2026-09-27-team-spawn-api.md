# Plan — team spawn API (spec: ../specs/2026-09-27-team-spawn-api-design.md)

1. 선언층: `deploy/nemoclaw/roles.yaml`(승인 역할 카탈로그), `deploy/nemoclaw/teams.yaml`(API 가 쓰는 팀 선언),
   `config.py` — `RoleSpec/RolesConfig`, `TeamDecl/TeamsFile`, `AgentSpec.team/allow_agents`, `load_assignments()` 가 teams 를 agents 로 병합.
   `manifests.py` — `maxSpawnDepth: 2`, supervisor 항목에 `subagents.allowAgents`, supervisor IDENTITY 에 팀 블록.
2. `teams.py` — `Patterner`(DirectPatterner: 프록시 경유 로컬 LLM JSON, KeywordPatterner 폴백), `TeamService.create/list/get/remove`:
   패터닝 → teams.yaml → assignments 재로드(브로커·진입점 갱신) → manifest → `agents apply` → `seed_sandbox` → 감사 `team`.
   fake 모드는 apply/seed 건너뜀.
3. `ask.py` — 카탈로그 = `ask.yaml tasks` + teams(supervisor 가 agent). `FakeTasks` 는 팀 task 에 KB domain 이 없으면 전체 노트 키워드 검색.
   `ask_api.py` — `/teams` 라우트(bearer), OpenAPI 재생성.
4. 스킬 `team-supervisor`, `task-verifier`; `tests/test_teams.py`; 데모 10; Makefile(`make test`, demo-10); README·ARCHITECTURE 갱신.
