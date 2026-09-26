# 후속 통합 후 task 재검증

이미 통합·완료된 task의 관련 소스/계약/명세가 바뀌면 기존 검증은 `stale`이고
task는 `verifying`/범위 예약으로 돌아간다. 과거 성공은 삭제되지 않으며 새 source의
성공을 의미하지 않는다. 이 절차는 기존 `taskctl`의 coordinator recovery를 사용한다.

## 재검증 baseline 발급

1. canonical `TASK_CONTROL_ROOT`에서 `status`와 해당 `context-pack`을 다시 확인한다.
   원래 submission/통합 증거, 후속 변경, 기존 프로세스와 부작용, 미통합 작업을 조사한다.
2. 현재 통합 대상 HEAD에서 **별도 clean feature worktree**를 준비한다. 과거 branch,
   dirty worktree, canonical checkout 자체는 재검증 baseline으로 허용하지 않는다.
   coordinator 전용 task도 이 분리 조건을 따른다. 생성/commit/merge는 자동 수행하지 않는다.
3. 조사한 사실만 다음 inspection JSON에 기록하고, 정확한 다음 행동을 handoff에 쓴다.

```json
{
  "old_process_stopped_or_fenced": true,
  "worktree_reviewed": true,
  "unintegrated_changes_reviewed": true,
  "side_effects_reconciled": true,
  "integrated_revalidation": true
}
```

```sh
python scripts/taskctl.py --control-root "$TASK_CONTROL_ROOT" recover TASK-ID \
  --session COORDINATOR-SESSION --expected-revision CURRENT-REVISION \
  --source /absolute/clean/source-worktree --new-session ASSIGNED-SESSION \
  --disposition resume --inspection-file /absolute/inspection.json \
  --handoff-file /absolute/handoff.md --reason 'Reviewed subsequent integrated changes'
```

위 대문자 인자는 실제 값으로 바꾼다. `--session`은 등록된 coordinator여야 하며
coordinator task의 `--new-session`도 coordinator여야 한다. 명시적
`integrated_revalidation`은 이미 통합된 submission과 실제 통합 증거가 있을 때만 허용된다.
증거의 task/source/commit 연결과 현재 target의 통합 commit ancestry를 확인한다.
worker commit의 cherry-pick 통합은 파일 hash 연결로 보존한다.

## 변경되는 것과 보존되는 것

- 새 generation과 현재 target HEAD baseline을 발급한다. 옛 generation/revision은 거절한다.
- `attempts.approaches`에 원래 claim, submission, integration/result/evidence,
  검증 summary와 최신 증거 참조를 보존한다. 불변 evidence 파일은 수정하지 않는다.
- 활성 integration result/evidence는 초기화한다. 새 worker 검증, `submit`, 새 target 검증,
  `integrate`, `close`를 모두 거쳐야 후속 task의 의존성이 풀린다.
- 소스 수정이 필요 없으면 무의미한 commit을 만들지 않는다. 명시 재검증 generation에
  한하여 **현재 clean target HEAD와 동일한 commit**의 owned 파일 hash를 제출한다.
  이는 새 실행 증거를 생략하는 기능이 아니다. 수정했다면 기존 scope/clean 검사도 적용한다.
- 미통합/만료/blocked 작업의 일반 recovery는 원래 baseline을 유지한다. `release`/`block`은
  claim을 history에 보존한다. 과거 기록에도 baseline이 없으면 자동 추정하지 않고 거절한다.
  이런 경우 coordinator가 이력을 조사하고 명시적인 후속 조치를 결정해야 한다.
- recovery는 이전 프로세스/외부 부작용을 자동 종료하지 않으며 새 검증을 실행하지도 않는다.
  target이 다시 바뀌면 재검증/범위 검토가 필요하다. 계약 또는 핵심 AC를 낮춰 통과시키지 않는다.

## 명세 이관 감사와 검증

source worktree에서 read-only 감사할 때는 canonical root를 명시한다. 선택한 경로가
잘못되었거나 복사본이면 `Store`의 repository/control-root 검사가 거절한다. fallback은 없다.
generated view 검사는 짧은 공통 lock 안의 snapshot만 비교한다. heartbeat를 멈출 필요는 없다.

```sh
TASK_CONTROL_ROOT=/absolute/canonical/control .venv/bin/python -m pytest -q tests/test_task_migration.py
.venv/bin/python -m pytest -q tests/test_taskctl.py
```

감사는 원래 D1~D3 24h에 승인된 E2E 추가 3h를 더한 D3 11h를 확인한다.
추가 기술 4.5h는 별도이며 직렬 핵심 추정은 31.5h이다(D4 통합 8h/본인 LLMOps 별도).
관리 회귀는 임시 Git/control fixture만 변경한다. 이 테스트의 성공은 RFA 제품/NAT/보안
시나리오 성공이 아니다. 제품 task의 필수 검증은 해당 명세대로 별도로 실행한다.
