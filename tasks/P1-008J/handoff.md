# P1-008J — 처리 상태 카드

## 구현 사실 (809d6dc)
- 단계 이벤트에 detail 추가·chat_stages에 보존: understanding(intent/rule), routing(route: kind/reason/label/task·team/considered/candidates+shared_subjects), team(기존 재사용 구성: pattern/state/runtime/역할별 agent_id·capabilities·tools), team_spawn(새 팀 패턴·예정 역할), team(새로 생성된 실제 구성), team_result(status/stop_reason/simulated/역할별 status·steps·tool_calls), completed(status/run/source). 메모 원문·답변·키는 detail에 없다.
- 라우터 route에 considered(검토한 owner Task 팀 수)·candidates 추가. 기존 팀 lifecycle 조회를 routing 직후로 옮겨 구성 단계를 실행 전에 표시.
- UI: #process-card sticky 카드(제목=요청 문장, 상태 진행 중/완료/확인 필요/결과 미확정, 단계 목록+detail 행). 스트리밍 중 live 갱신, result로 확정, 새로고침 시 마지막 턴 복원, 오류 시 error 행. 버블 타임라인은 유지.
- 공통 DTO/graph/adapters 변경 없음. 실제 게시/모델 호출 없음.

## 검증
- 개발1: 기존 stage 순서 assertion 2건이 새 team/team_result 단계로 실패(예상) → 테스트 갱신 및 detail 검증 추가. 개발2: outcome 변수 UnboundLocalError(존재 task 경로) → 초기화 보수. 이후 scoped 39 passed.
- 브라우저 procedure 1차: live_snapshots(첫 관찰 시 단계 0개) 과잉 assertion으로 실패 → "진행 중" 표시 확인으로 대체; 2차는 같은 서버 재사용으로 이전 Task가 남아 실패(fixture 오염) → 새 data-dir. 3차 통과.
- worker state-worker-01: V1 39 passed, V2 Chrome154 통과(빈 상태 카드, 진행 중 live, 메모 4단계, 새 팀 7단계·역할 구성/결과 행, 재사용 6단계·후보, 카드가 버블 위, 새로고침 복원, 390px, JS/outbound 0).

## 다음 행동
main 통합 → target 검증 → close → 사용자 8780 재시작.
