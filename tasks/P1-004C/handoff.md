# P1-004C — 결정적 한국어 cue 표 추출 품질

## 구현 (wip/P1-004C 107ad01, e2e_worker)

- 버전 붙은 규칙 표 `extract-rules-v2`: 결정·행동·마감·상태·blocker cue.
- 문장 단위로 추출한다.
- 후보 key는 revision과 언급이 바뀌어도 안정적이며, 중복 후보는 superseded로 처리한다.

## 측정 (같은 seed·fixture, n=3)

| 항목 | 이전 | 이후 |
|---|---|---|
| E2E-01 추출 점수 | 0.6667 | 1.0 |
| E2E-04 후보 coverage | 0.3333 | 1.0 |
| E2E-04 blocker 우선순위 | 0.0 | 1.0 |

## 검증

- 신규 테스트 13 passed.
- 전체 1173 passed, 21 skipped(opt-in live). control 테스트 9건은 worktree 환경 한정 실패이며 base와 동일하다.

