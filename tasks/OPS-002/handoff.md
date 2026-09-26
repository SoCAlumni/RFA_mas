# OPS-002 handoff — 2026-09-26

## 목표 / 관련 AC

AC1~AC3: 후속 정상 통합으로 stale된 task에만 명시적 revalidation baseline을 발급하고
원래 provenance/fencing/scope/새 worker·integration 검증/close gate를 보존한다.
AC4: 명시 canonical root read-only migration 감사와 승인된 E2E 공수 증가를 검증한다.

## 현재 사실 / 변경 파일

- `scripts/tasklib/cli.py`: inspection의 `integrated_revalidation: true`일 때 과거 integrated
  submission/target evidence/source binding을 확인하고 clean 현재 target HEAD의 별도
  feature worktree에서 새 baseline/generation 발급. coordinator task도 feature 분리 적용.
- 원래 claim/submission/integration/evidence/summary는 attempts.approaches에 보존한다.
  활성 target result/evidence는 초기화하여 새 worker+target 실행 없이는 close할 수 없다.
- no-op 재검증은 정확한 current clean target commit의 모든 owned source hash를 제출한다.
  새 코드 commit을 억지로 만들지 않지만 현재 commit과 새 실행 증거는 필수다.
- 미통합 일반 recovery는 원래 baseline을 유지한다. release/block의 claim을 history에
  보존한다. 예전 운영 기록에도 baseline이 없으면 추정하지 않고 오류를 반환한다.
- `tests/test_taskctl.py`: 실제 temp Git의 통합→후속 A/B 변경→stale→복구→새 증거→close,
  no-op/owned-fix, dirty/old branch/no evidence/old generation/pending scope 세탁 거절 회귀.
- `tests/test_task_migration.py`: TASK_CONTROL_ROOT 명시 경로 사용 및 잘못된 복사 root
  거절 유지. view 비교는 짧은 store lock. D3=11h/P0-026=4h 명시, 다른 gate/원문 보존.
- `docs/TASK_REVALIDATION.md`: 정확한 기존 recover 명령, inspection, 제한/증거 절차.

## 결정과 이유 / 인터페이스

새 framework/상태/자동 merge는 없다. 기존 inspection JSON opt-in만 추가했다.
미통합 작업에는 opt-in을 거절하여 후속 target baseline으로 scope 밖 변경을 세탁하지 않는다.
원래 worker commit은 cherry-pick될 수 있어 target 파일 hash로 연결하며, 원래 target
integration commit이 현재 target의 ancestor인지 확인한다. 기존 immutable evidence는 보존한다.
작업관리 성공은 제품 기능/NAT/OpenShell/시나리오 성공과 다르다.

## 실제 검증 / 증거

- evidence: `.agent/evidence/OPS-002/recovery-01/source.json`, `result.json`.
- `.venv/bin/python -m pytest -q tests/test_taskctl.py`: 66 passed in 45.40s.
- 명시 canonical TASK_CONTROL_ROOT에서 `.venv/bin/python -m pytest -q tests/test_task_migration.py`:
  17 passed in 9.13s. 사용자 데이터 변경 없이 read-only 감사, mutation은 임시 fixture만 사용.
- `git diff --check` 통과. owned Python 3파일 Ruff check 통과.
- 제품 E2E 10개/외부 서비스/실제 provider는 이번 task에서 실행하지 않았다.

## 오류 / 재시도 / blocker

Ruff 초기 preflight는 E501 긴 줄 9개(cli.py 564; test_task_migration.py 134;
test_taskctl.py 1101/1108/1111/1130/1141/1159/1169)로 실패했다.
owned 파일에 formatter 적용 후 1회 재시도에서 All checks passed. 원문 오류 요약과 수정
기록을 immutable result의 report.preflight에 보존했다. 필수 V1/V2는 첫 attempt에서 통과.
현재 구현 blocker 없음. 과거 baseline 없는 release 기록은 자동 복구할 수 없으며
coordinator의 명시적 Git/history 조사와 후속 조치가 필요하다. source/기존 증거 삭제 금지.

## 정확한 다음 첫 행동

Coordinator가 이 feature commit과 owned diff를 확인하여 통합 대상에 반영한다.
현재 target에서 새로운 integration evidence로 V1/V2 실행 → integrate → close한다.
그 다음 P0-014/P0-027처럼 이전에 통합된 stale task를 current clean feature worktree로 준비하고
inspection에 integrated_revalidation=true를 넣어 recover한 뒤 해당 제품 검증을 새로 수행한다.
과거 성공이나 OPS-002의 관리 테스트를 해당 제품 task의 검증 증거로 재사용하지 않는다.
