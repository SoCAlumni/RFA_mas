# Worker 시작 프롬프트

Coordinator에게 개발 task ID, 고유 session ID, source worktree/branch, canonical root를 전달받아 사용한다. 아래 P0-018은 예시이며 지정값으로 대체한다. 현재 baseline 미등록 상태에서는 구현을 시작하지 말고 coordinator에게 OPS-000을 보고한다.

```sh
export TASK_CONTROL_ROOT=/Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" status --workstream agents
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" show P0-018
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" context-pack P0-018
```

적용 지침/짧은 공통 맥락/실행 규칙을 읽은 뒤 이 task의 pack·handoff·직접 선행 결과만 깊게 읽는다. 현재 spec/contract/ready와 source baseline을 확인한 다음 claim에 session/source/expected-revision을 넣는다. 반환 generation을 이후 모든 갱신에 넣고 revision은 매번 최신 값으로 조회한다.

owned scope만 구현한다. 다른 task·공유 계약·상대 service DB를 임의 변경하지 않는다. 긴 테스트 중 수동 heartbeat, 종료/실패/전환 시 update handoff. 검증 전 begin-evidence → 계획된 테스트를 source worktree에서 직접 실행 → 실제 결과 report → record-evidence 순서다. 불필요한 YAML command 자동 실행, secret/private 로그 수집 금지.

필수 검증 통과와 승인된 source commit 준비 후 submit한다. done이 아니라 verifying/pending이며 예약을 임의 해제하지 않는다. 오류/skip/미수집은 성공 아님. blocked인 경우 실패 evidence와 다음 해소 조건을 기록한다.

응답 형식: task/spec revision/claim generation / 실제 변경 / AC별 결과·evidence / 한계·pending effects / 정확한 다음 첫 행동. 다른 세션에서도 canonical CLI만 사용한다.
