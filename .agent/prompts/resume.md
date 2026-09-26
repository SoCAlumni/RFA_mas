# Resume — 선택 task만 재개

새 세션에서는 이전 대화를 읽었다고 가정하지 않는다. 적용 지침과 공통 규칙은 그대로 따른다. Coordinator가 전달한 TASK_ID, SESSION_ID, SOURCE_WORKTREE를 확인한다. 아래 P0-014는 지정 ID로 바꾼다.

```sh
export TASK_CONTROL_ROOT=/Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" show P0-014
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" context-pack P0-014
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" status
```

현재 revision/generation/owner/lease, spec_revision, global/contract digest, source branch/HEAD/dirty manifest를 확인한다. handoff의 현재 사실·최근 실패·미완료 부작용과 직접 선행 결과만 읽고 기록된 첫 행동을 수행한다. 보고서보다 원본 code/evidence가 우선한다.

같은 소유자·유효 claim이면 heartbeat 후 계속한다. 세션이 바뀌었거나 만료/계약변경이면 coordinator inspection + recover 없이 옛 token/generation으로 진행하지 않는다. 제출 예약 상태라면 구현을 다시 시작하지 말고 통합 진행을 조회한다. stale 증거는 재사용하지 않는다. 자동 프로세스 종료/외부 부작용 취소는 없다.

pack 분리나 compact 지시 자체는 context 초기화가 아니다. 실제 새 chat/worker를 만들었을 때만 fresh context라고 표현한다. 새 worker가 없다면 이 문서를 복사해 수동 새 세션을 연다.

응답 형식: 확인한 task/claim/spec/계약/source / blocker·pending effects / 재개할 정확한 첫 행동과 검증.
