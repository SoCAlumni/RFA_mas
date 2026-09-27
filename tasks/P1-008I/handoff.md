# P1-008I — Task 팀 단위 담당 선택

## 구현 사실 (bfc4bc3, worktree P1-008I-teams)
- ChatMessage.task_id 추가. 라우터는 명시 task_id를 owner catalog에서 재검증(active/ready만 허용, 다른 owner/없음→not_found, 사용 불가→task_unavailable). 거절은 대화 ledger에 남지 않는다.
- chat_intent에 task_run(조사해/검증해/분석해/비교해/실험해/연구해/벤치마크 돌려/run benchmark/investigate) 추가. 기존 Task 주제 매칭이 있으면 그 팀 재사용, 없으면 core team 경로(TeamExecutionRequest, 패턴은 BENCHMARK_TERMS 유무)로 정확히 하나의 Task+팀 생성 후 /v1/runs/{id}/team으로 실제 task/team ID를 route에 기록. 메모/일반 질의는 여전히 팀을 만들지 않는다.
- GET /ui/api/chat/assignees: owner Task 팀 목록(goal/pattern/domain/status/selectable). 드롭다운은 Task 팀 optgroup 우선, 자료 공간(TRIV3/양자화)은 보조. 채팅 응답에 Task/팀/패턴/simulated 표시. 단계 라벨: 요청 이해 중→담당자 확인(적합한 기존 Task 팀 없음 · 새 Task 팀 구성)→Task 팀 실행 준비→Task 팀 결과.
- 공통 DTO/graph/adapters/settings 변경 없음. LLM 라우팅/토큰 스트리밍/실제 게시/NVIDIA 호출 없음. 실험 수치는 mock(simulated).

## 검증
- 개발1: 신규 test 1 fail — 타 owner 팀을 team_factory로 만들 때 권한 없음(team_selection_denied). 테스트 fixture를 owner registry 직접 삽입으로 바꿔 해결(제품 오류 아님). 이후 9/9, scoped 39 passed/0 failed/0 skipped.
- worker teams-worker-01: V1 39 passed, V2 Chrome154 통과(빈 목록 안내, 메모 무생성, 명시 요청 1팀 생성, 드롭다운 Benchmark 표시, 관련 질의 자동 배정, 명시 선택, no-match 비서, 5 NDJSON 스트림, 새로고침 복구, 390px, JS/outbound 0). 임시 18780 서버·합성 data-dir만 사용.

## 다음 행동
main fast-forward 통합 → target 검증 → close. 이후 사용자 8780 서버 재시작 후 샘플 Task 팀 2개를 채팅 API로 시드.
