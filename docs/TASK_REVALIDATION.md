# 후속 통합 후 task 재검증

이미 통합·완료된 task의 관련 소스/계약/명세가 바뀌면 기존 검증은 `stale`이고
task는 `verifying`/범위 예약으로 돌아간다. 과거 성공은 삭제되지 않으며 새 source의
성공을 의미하지 않는다. 이 절차는 기존 `taskctl`의 coordinator recovery를 사용한다.

## 완료된 consumer의 계약 갱신

새 provider artifact를 통합한 후 `publish-contract`를 실행하면 기존 consumer의
과거 검증이 stale일 수 있다. **활성 claim이 없고 이미 integrated였던 stale 예약**은
이유만으로 계약 발행을 막지 않는다. 도구는 원래 submission/result/evidence/source
manifest 연결, 기록된 통합 commit의 현재 canonical target ancestry와 target branch를
확인한다. 활성 claim, 미통합 pending 제출, 증거가 없거나 변조된 예약은 계속 거절한다.
명령의 coordinator/project digest/schema·fixture evidence 요구도 그대로다.

1. 기존 `publish-contract`로 검증한 artifact를 발행한다. consumer는 draft/stale이며
   예약·이전 submission·증거를 유지한다. artifact 바이트가 같아도 버전이 바뀌면 같다.
2. consumer의 최신 revision을 조회하고 `edit-spec`으로 **기존 contract_refs의 ID와
   role를 유지한 채** 발행 registry의 실제 version/digest만 수락한다. 이 예약 상태에서는
   `contract_refs`, `spec_state`, `unresolved` 세 필드만 허용한다. provider 전환·계약 추가/
   삭제·scope/AC/의존성 변경은 이 경로로 처리할 수 없다.
3. `unresolved`에서 수락한 현재 계약의 자동 `Re-read …` 안내와 publisher가
   `contract_changes`에 해당 consumer/계약/version/notice로 기록한 중간 발행 안내만
   제거할 수 있다. 따라서 v2/v3를 연속 발행한 뒤 최신 v3를 직접 수락할 수 있다.
   ref 갱신과 해당 안내 제거는 같은 patch에서 함께 해야 한다. ref만 수락하고 안내를
   남기는 중간 상태는 거절해 이후 정리할 수 없는 예약을 만들지 않는다.
   독립적인 미결정 사항은 유지하고 draft로 둔다. 미발행/파일과 불일치하는 digest,
   unresolved가 남은 ready, 미갱신 계약이 있는 ready는 거절한다. 발행 이력으로 확인할 수
   없는 과거 안내나 다른 명세 판단은 prefix로 자동 삭제하지 말고 coordinator가
   별도로 조사·인계한다.
4. 수락은 새 구현·검증·완료가 아니다. 아래 inspection/recovery → 새 worker evidence →
   submit → 새 target evidence → integrate/close를 거쳐야 의존성이 풀린다. 이전 evidence
   파일을 수정하거나 재검증 예약을 해제하지 않는다.

이 예외는 같은 호스트의 신뢰된 coordinator 운영 프로토콜이다. 임의 사용자 인증이나
변조 방지 서명을 새로 제공하지 않으며, 자동 merge·검증 실행도 하지 않는다.

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

이미 통합된 여러 task가 같은 경로/자원을 보유했다면 계약 변경으로 모두 예약될 수 있다.
명시적인 `integrated_revalidation` recovery에만, 충돌 상대가 **claim 없는 stale 통합
예약이고 동일한 역사적 evidence/ancestry 검사에 통과**할 때 순서상 첫 재검증을 허용한다.
상대의 예약·증거는 그대로 남는다. 첫 claim이 생긴 후에는 두 번째 overlapping recovery가
거절되고, 첫 task의 새 검증·submit·target 검증·close가 끝나야 다음을 시작할 수 있다.
만료 claim도 활성 점유로 취급한다. 일반 claim/recover, 미통합 pending 예약 또는 변조된
역사 증거에는 이 예외가 없다. 병렬 재검증이나 자동 scope 양도를 허용하는 기능이 아니다.

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

초기 이력은 D1~D3 24h에 승인된 E2E 추가 3h(D3 11h), 추가 기술 4.5h를 더한
직렬 31.5h이다. 이 수치를 현재 추정으로 고정하거나 과거 기록에서 삭제하지 않는다.
2026-09-26 실제 사전 조사로 P0-017은 0.5→1h, P0-019는 1.5→2h,
P1-006D는 0.5→2h로 승인 변경됐다. 이 중간 계획은 D1=8.5h, D2=8.5h,
D3=11h와 추가 기술 6h, 직렬 합계 **34h**이며 과거 기록으로 보존한다.
후속 승인된 P1-006의 1→2h와 P0-020의 1.5→3h를 반영한 현재 감사는
**D1=8.5h, D2=10h, D3=12h, 추가 기술=6h, 직렬 합계=36.5h**를 검사한다.
합계는 canonical task 원본과 project milestone 구성에서 직접 계산한다.
D4 통합 8h와 본인 LLMOps 3.5h는 별도이며, 병렬 실행을 근거로 직렬 합계를 축소하지 않는다.
앞선 기대값 갱신은 이관 테스트 두 곳의 옛 합계 불일치에 대한 보수였다. 이번 후속은
`.agent/evidence/P0-019/full-regression-01.json`의 실제 전체 회귀 596 passed/1 failed
(D3 기대11h와 실제12h 불일치)을 첫 실패 근거로 보존하고 로그 기반 최대3회로 보수한다.
이전 실패를 새 성공으로 덮어쓰지 않는다. P0-020의 추가 승인 증가도 같은 현재 합계에 반영한다.
수정 대상은 일정 테스트와 이 문서뿐이며 CLI/framework 구현을 확장하거나 변경하지 않는다.
원문 checksum·ID·완료 이력·NAT 독립성·mock/real 최종 gate는 그대로 검사한다.
관리 회귀는 임시 Git/control fixture만 변경한다. 이 테스트의 성공은 RFA 제품/NAT/보안
시나리오 성공이 아니다. 제품 task의 필수 검증은 해당 명세대로 별도로 실행한다.

## 통합 후 재검증 cascade 운영 보조 (2026-09-26 추가)

공유 소스(contracts, local.py, service 등)나 RFA-EXTENDED 계약이 바뀌면 그 파일을 fingerprint에 포함한
done task가 verifying/stale로 바뀐다. 이는 의도된 동작이며 영향 AC를 다시 검증해야 의존 task를 claim할 수
있다. coordinator는 `.agent/input/tc.py`(개발 운영 보조, 제품 코드 아님)로 다음 절차를 반복한다.

1. `accept`: consumer의 contract_refs를 현재 발행 version/digest로 `edit-spec` 수락한다(예약된 task는
   계약 수락 필드만 허용된다).
2. `recover --disposition resume`와 `integrated_revalidation: true` inspection으로 현재 통합 HEAD의 clean
   feature worktree에 새 generation claim을 만든다(과거 generation의 늦은 요청은 거절된다).
3. `verify`: task의 검토된 pytest 계획 argv만 `env -i`(PATH/HOME 임시/TASK_CONTROL_ROOT)로 실행하고 실제
   수집·통과 수로 report를 만든 뒤 begin/record-evidence를 호출한다. skip·미수집·timeout은 passed가 아니다.
   manual 계획은 실제로 다시 확인한 관찰 문장을 요구한다.
4. worker 통과 후 submit → canonical target에서 같은 계획으로 integration evidence → integrate → close.

주의: 동시에 여러 전체 회귀를 실행하면 120초 plan timeout을 넘을 수 있다(2026-09-26 OPS-002 r3에서 실제
발생, exit -9). 시간 한도를 늘리지 않고 부하를 줄여 단독 재실행한다. 일반 recovery(비 revalidation)로 바뀐
작업은 claim baseline 이후 소유 경로의 실제 변경만 submit할 수 있으므로, 새 baseline 커밋을 섞지 않는다.

OPS-003(2026-09-27): integration record-evidence는 이제 begin-evidence에서 캡처한 manifest head를
result.head로 기록한다. 다른 세션이 검증 중 fingerprint 밖 파일을 commit해도 binding이 어긋나지 않는다.
이전 도구가 기록한 과거 result.head는 manifest head의 후손이고 fingerprint·파일 hash·spec·contract가
같을 때만 revalidation 근거로 인정한다(P1-001 post-retrieval-target: 3a1a5ee 캡처, caf7bd2 기록).

OPS-004(2026-09-27): cProfile 측정에서 `taskctl ready` 한 번이 약 22초였고, 그중 약 20초가
done task마다 같은 target worktree에 git 조회를 반복한 `complete()→valid_evidence()→source_manifest()`
(git subprocess 1,551회)였다. 이제 `cli.main()`이 호출 단위 `git_memo()`를 열어 같은 (경로, argv)의
읽기 전용 git 결과(성공 stdout 또는 거절)를 그 호출 안에서만 재사용한다. taskctl은 git을 변경하지 않으므로
한 호출 안의 판정은 하나의 snapshot으로 더 일관된다. 호출이 끝나면 memo를 비운다. `execute()`를 직접 부르는
in-process 호출과 테스트에는 memo가 없어서 호출 사이의 source 변경(tampered head, dirty tree)을 기존대로
탐지한다. fingerprint 비교·파일 hash·fencing·scope 규칙은 바꾸지 않았다. 같은 control 상태에서 CPU 시간이
약 11.5초에서 1.7초로 줄었다. lock 대기 시간은 이 수치에 포함되지 않는다.


## OPS-005 이후 재검증 운영 절차 (2026-09-27, OPS-002 갱신)

- 계약 발행이나 공유 소스 변경 뒤 전체 cascade를 바로 돌리지 않는다. 다음에 claim할 task의 `depends_on` 중
  stale인 task만 `REVAL_ONLY=<ID,...> revalidate_par.py` 또는 `tc.py revalidate`로 먼저 재검증한다.
  stale 통합 예약은 새 claim의 충돌 사유가 아니지만 dependency gate는 그대로다(OPS-005).
- 최종 acceptance(P0-026 closeout, P1-009) 전에는 모든 통합 task를 현재 target에서 재검증한다(전체 1회).
- `TASK_EXECUTION_RULES.md`, `docs/PROJECT_CONTEXT.md` 같은 global context 문서는 모든 task의 context digest에
  들어가므로 바꾸면 모든 통합 task가 stale이 된다(2026-09-27 OPS-005 merge에서 실제 발생). 절차 보완은
  가능하면 이 문서(개별 task context가 아님)에 적고, global 문서 변경은 최종 전체 재검증 전에 묶는다.
- 재검증과 통합 merge를 동시에 돌리지 않는다. 재검증 도중 target이 이동하면 worker 변경 없이 현재 target
  HEAD로 clean fast-forward하고 같은 계획으로 worker evidence를 다시 캡처한 뒤 제출한다(OPS-005).
- 도구 셸에서 `nohup … &`로 띄운 장시간 작업은 셸 세션 종료 때 함께 종료되었다(2026-09-27 실제 발생:
  재검증과 통합이 로그 없이 중단). 장시간 cascade/통합은 유지되는 실행 세션에서 돌리고 주기적으로 조회한다.
- 사건 기록: OPS-002의 post-ops004 재검증 claim은 P1-008C 통합으로 target이 이동해 submit이 거절되었고,
  claim 중인 task는 revalidation recover가 불가했다. 소스 변경이 없는 재검증이었으므로 discard로 정리했고,
  같은 이유로 막힌 OPS-003 stale 예약도 discard했다. 두 task의 과거 통합 이력은 attempts.approaches에 남아
  있고, 이 문서 갱신(OPS-002)과 FF 재검증 binding 회귀(OPS-003)로 다시 완료한다.

