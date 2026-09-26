# P1-006B — Persona 시뮬레이션·버전 회귀 비교

- 구현(wip/stack 911f8a8, 8079999; quality_worker 개발)
  - persona_regression_v2 24사례(4 identity × 6 상황), C01–C12·기존 24 ID crosswalk.
  - run manifest(input/dataset digest, policy, code/evaluator/template/runtime 버전).
  - 같은 dataset digest·seed·policy·mode의 서로 다른 두 실행만 비교하고, 아니면 not comparable을 반환한다.
  - CLI: `evaluate --dataset persona-regression-v2`, `evaluate-compare`.
  - 8079999는 P1-005 이후 staged-context 경계에도 누출 주입을 추가한 테스트 전용 변경이다.
- 측정(simulated, mock 모델, 같은 dataset·seed 29·policy local-v1)

  | 기준 | pass | fail | security fail | release gate |
  |---|---|---|---|---|
  | a0a9347 기준 | 19 | 5 | 4 | fail |
  | stack tip | 23 | 1 | 0 | fail |

  - stack tip의 남은 1건은 colleague-evidence-insufficient(기능 규칙)다.
- not_run: 실제 모델 비교, Judge 의미 품질.

