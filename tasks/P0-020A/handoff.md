# P0-020A — result_analyst 결정적 후속 비교

- 구현(wip/stack ad6fab0)
  - run은 내용 기준으로 정렬하고, 무작위 ID는 완전 중복일 때만 tie-break로 쓴다.
  - baseline을 모든 비잠정 후보와 비교한다. 각 비교에 source_id·source_revision을 넣는다.
  - tool 예산 한도에서는 남은 후보를 skipped(tool_budget)로 보고하고 run을 완료한다. coordinator가 이 동작 변경을 승인했다.
- 개발 검증: test_team_execution 14 passed. stack 구간 126 passed.

