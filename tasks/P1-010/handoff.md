# P1-010 — 승희 knowledge 계약 facade와 다영 실행 형태 정합

- 목표 / AC1~3: RFA_module contracts/knowledge.openapi.yaml(819053f)의 GET /tasks, POST /tasks/{id}/ask를 core 위에서 제공. 공개 근거만 답변, CLI host/port/audience.
- 구현 사실: 새 패키지 src/rfa_mas/knowledge_facade/{contract,service,app}.py. 정책(PolicyPort retrieve/target audience) → 허용 audience 검색(RetrievalPort) → 기존 share_egress_filter → ModelPort → canary 치환. 근거 없음은 answer ""/sources []/confidence 0(승희 ask_knowledge의 재선택 규칙과 호환). GET /tasks는 KB 도메인(triv3, quantization_research)을 task로 투영. 404 본문 {error: not_found, id}. 공통 DTO/bootstrap/graph/settings 변경 없음. src/rfa_mas/cli.py에 `knowledge-facade` 명령(기본 127.0.0.1:8791, --audience public|company|business_unit; non-public은 KNOWLEDGE_FACADE_API_KEY bearer 필수)과 `openapi --app knowledge-facade` 추가.
- 변경 파일: 위 패키지, src/rfa_mas/cli.py, tests/test_knowledge_facade.py, tests/helpers/openapi_check.py(최소 OpenAPI 검사기), fixtures/contracts/rfa_module/{knowledge,review}.openapi.yaml+PINNED.md(승희 원본 사본), docs/api/knowledge-facade.openapi.json(생성), docs/TEAM_ALIGNMENT.md. worktree rfa_mas_worktrees/P1-010-align, branch feat/P1-010-teammate-alignment, commit 1532dcc(base efa063a).
- 결정: frozen 디렉터리(src/rfa_mas/contracts/, docs/contracts/) 밖에 배치해 fingerprint stale 회피. public facade는 무인증(공개·공유 가능 근거만 반출), 그 외 audience는 bearer 키 없이는 앱 생성 자체를 거절. owner/private audience는 ValueError.
- 검증: worktree에서 tests/test_knowledge_facade.py 7 passed(승희 yaml schema 호환, canary/비공개/사내/팀 자료 미노출, 빈 답변, bearer 401, CLI 종료코드 2, operationId 일치). ruff check/format 통과. 실제 RFA_module 워크플로·샌드박스 정책 통과·실제 모델 호출은 not_run.
- 실패/blocker: 첫 commit이 frozen 경로에 파일을 두어 freeze 경고 → 경로 이동 후 amend. pending side effect 없음.
- integration: main이 efa063a→cc86868(P1-008G)로 이동. 현재 main HEAD에서 새 worktree에 cherry-pick 후 worker/target evidence 재캡처 → submit → ff/merge → integrate/close.
- 다음 행동: tc.py intworker P1-010 <session> tasks/P1-010/handoff.md 1532dcc. 이후 다영 modules.yaml의 knowledge run 명령 교체 요청, 승희 KNOWLEDGE_URL 교체 실연은 별도 gate(P1-008A 계열).
