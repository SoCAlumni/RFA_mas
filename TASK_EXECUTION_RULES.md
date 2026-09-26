# 개발 task 실행 규칙

이 문서는 제품 Task/Team/Run이 아닌 개발 세션 운영 규칙이다. 먼저 AGENTS.md와 docs/PROJECT_CONTEXT.md를 읽는다. 전체 task 상세를 context에 넣지 않는다.

## 원본과 시작

Canonical root: `/Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas`. 모든 세션은 이 절대 경로를 `TASK_CONTROL_ROOT`로 전달받는다. source worktree의 tasks 복사본은 원본이 아니다. `.agent/control.json`의 repository identity/root가 맞지 않거나 접근할 수 없으면 CLI는 실패하며 로컬 복사본으로 fallback하지 않는다.

```sh
export TASK_CONTROL_ROOT=/Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" status
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" ready
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" context-pack P0-014
```

task.yaml이 명세·상태 원본, handoff는 재개 요약, project.yaml은 공통 규약이다. index/state/TASKS.md는 읽기용 파생 view이며 직접 수정 금지. schema는 `scripts/tasklib/schema.py`. 최초 이관 외 명세 변경도 coordinator의 `edit-spec`을 이용한다. source 변경 commit에는 stale tasks/global view/handoff를 섞지 않는다. canonical 상태 Git 이력은 coordinator만 관리한다.

현재 Git은 초기화됐지만 HEAD 커밋이 없어 source worktree baseline은 **unregistered**다. OPS-000에서 승인된 초기 기준 또는 기존 checkout을 확인한다. 이 요청은 사용자 변경 commit/stash/reset 권한이 아니다. CLI의 planning_ready는 설계/의존성 준비, executable은 baseline·reservation까지 포함한다. planning-ready P0-014/P0-027도 baseline 전에는 claim 불가다.

Git 기준이 마련되면 coordinator는 원본 보존·secret 미추적·clean branch/HEAD·repository marker를 검사한다. `project-digest` 후 `adopt-baseline --session rfa-coordinator --source <통합 checkout> --expected-project-digest <값> --inspection-file <검사 JSON>`으로 등록한다. JSON의 `user_changes_preserved`, `source_reviewed`, `no_secrets_tracked`, `control_files_excluded_from_worker_commits`는 실제 확인 후 true로 기록한다. 기준 등록은 코드/상태 commit이나 merge를 대신하지 않는다.

## 선택과 병렬 배정

- `ready --workstream agents`처럼 필터한다. 그룹/milestone은 project의 ID 묶음이며 claim하지 않는다. spec=draft, unresolved, 선행 미통합, deferred/cancelled/blocked, 미등록 baseline이면 실행 불가다.
- 의존성은 선행의 실제 integrated+유효 검증+done이다. 기존 완료 13개는 해시 고정한 이관 전 로컬 기반 증거의 한정 예외다. 새 worker passed만으로 의존성이 해제되지 않는다.
- 별도 feature worktree/branch는 현재 통합 HEAD에서 만든다. 필요한 미커밋 변경이 빠진 checkout, 오래된 HEAD, 새 claim의 dirty feature tree는 거절한다. `git worktree add -b <개발 branch> <개별 경로> <확인한 HEAD>`는 baseline 승인 후 coordinator가 수동 수행한다. 자동 worktree 생성/merge 없음.
- 동시 worker 기본 2, 최대 3. 각 task의 owned_paths와 shared_resources를 같은 lock 안에서 검사한다. 부모 경로/glob의 겹침이 불확실하면 충돌로 본다. 동일 source worktree 동시 실행 금지.
- DTO/ports, local.py migration, bootstrap, pyproject/lock/settings, root conftest는 coordinator 직렬 소유다. read_only_paths는 임의 변경 금지. DB/port/output은 tmp_path·attempt 단위로 분리한다.
- P0-014 후 P0-015(세션/coordinator)와 P0-018(순수 selector/worker)은 파일이 분리되어 병렬 가능하다. selector의 router 연결은 P0-020에 남겼다. KB/local.py, 세션/local.py는 병렬 불가. 현실적인 범위를 늘리기 위해 신규 MSA/framework를 만들지 않는다.

## claim·revision·lease

```sh
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" claim P0-018 \
  --session worker-agents-1 --source /absolute/feature-worktree --expected-revision 1
```

예시 revision은 항상 최신 show 출력으로 대체한다. 모든 update는 session + generation + expected-revision을 요구한다. 오래된 revision/소유자/generation은 거절한다. generation은 비밀 token이 아니라 fencing 번호다. 같은 OS 계정의 신뢰된 개발 세션을 위한 도구이며 파일 ACL/적대적 사용자 인증 서비스를 대체하지 않는다.

lease 900초, 권장 heartbeat 60초. `heartbeat ID --session ... --generation ... --expected-revision ...`을 수동 또는 별도 터미널에서 명시적으로 실행한다. 자동 heartbeat/background daemon/push는 구현하지 않았다. 긴 테스트는 foreground lock 없이 실행하고 새 revision을 다시 조회한다.

만료는 recovery_required이며 기존 scope를 계속 점유한다. 이전 프로세스가 멈췄다고 가정하지 않는다. `recover`는 coordinator만 사용하며 old_process_stopped_or_fenced / worktree_reviewed / unintegrated_changes_reviewed / side_effects_reconciled를 실제 확인한 inspection JSON, handoff, reason, 새 session/source 및 disposition=resume/discard를 요구한다. generation을 교체해 옛 update/submit/통합 요청을 차단한다. 외부 부작용/프로세스를 자동 종료하지 않는다.

`block`/`release`는 이유·해소 조건·handoff를 저장하고 claim은 반납하되 미통합 scope 예약을 유지한다. coordinator recovery로 명시적인 인계/폐기를 판단해야 예약이 풀린다. 외부 의존으로 처음부터 blocked/deferred인 unreserved 작업은 `unblock --reason` 후에도 readiness를 다시 검사한다. 독립 작업은 계속할 수 있다.

## 실행·증거·제출

1. context-pack의 spec_revision·global/contract digest·baseline을 확인한다. handoff가 없으면 docs/templates/HANDOFF.md를 사용한다. 직접 선행의 결과만 읽고 필요한 소스만 탐색한다. pack은 snapshot이며 새 대화/context reset을 수행하지 않는다.
2. owned scope에서 구현한다. 의미 있는 변경·실패·통합 요청·종료 때 handoff를 `update --handoff-file ... --result ... --next-action ...`로 반영한다. 운영 YAML을 직접 덮어쓰지 않는다.
3. `begin-evidence ID --attempt <새 ID> --session ... --generation ... --expected-revision ...`으로 검증 전 source manifest를 불변 저장한다. 테스트는 source worktree에서 task의 argv/cwd/timeout을 검토하고 직접 실행한다. CLI가 YAML의 임의 명령을 자동 실행하지 않는다.
4. 안전한 JSON report를 `record-evidence ... --attempt <동일 ID> --report <파일>`로 기록한다. 실제 reviewer/started_at/finished_at/checks가 필수. check에는 id/result/assertions가 있고 command는 argv/cwd/exit_code/log_excerpt, pytest는 collected/passed/skipped/failed/errors/xfailed/deselected 수를 기록한다. manual/API/UI는 observed/procedure_performed가 필요하다. 형식 예는 tests/test_taskctl.py의 report fixture를 보되 그 합성 성공을 실제 검증에 복사하지 않는다.
5. 테스트 미수집·skip·환경 오류는 passed가 아니다. report는 실행자의 명시적 attestation이며 도구가 실제 테스트 실행을 증명/재생한다고 주장하지 않는다. coordinator는 로그/실행 결과를 대조한다. 원문/비밀/긴 로그는 기본 수집하지 않는다. 민감 정보가 없는 assertion 요약만 report에 넣는다.
6. evidence는 `.agent/evidence/<ID>/<attempt>/source.json`, `result.json`에 새 파일로만 저장한다. 덮어쓰기 금지, 재실행은 새 attempt. source hash는 관련 tracked/staged/unstaged/untracked 내용과 index blob, HEAD/branch/worktree, spec/contract를 연결한다. `.env*`(example 제외), key/credential, runtime/vendor/대형 자료, generated task 상태·evidence는 제외한다. heartbeat는 제품 fingerprint를 무효화하지 않는다. 도구 자체 검증에는 scripts/tasklib/schema/tests를 포함한다.
7. 기본 수정/검증 cycle 3회, 같은 fingerprint 실패 반복 2회 후 block/handoff한다. coordinator가 새 접근 이유를 남긴 recover만 cycle을 새로 시작하며 이전 counts/evidence는 보존한다. AC를 낮추거나 검증을 제거해 통과시키지 않는다.
8. 필요한 코드 commit은 사용자가 승인한 개발 workflow에서만 수행한다. clean artifact + 유효 source/spec/contract evidence + handoff로 `submit`한다. status=verifying, verification=passed, integration=pending. claim은 없어져도 owned_paths/shared_resources 예약은 남는다.

## 통합·완료·변경

Coordinator는 제출 generation/HEAD/변경 파일/범위/evidence를 확인하고 수동 merge/cherry-pick 등 승인된 방식으로 반영한다. 도구는 자동 merge하지 않는다. source 상태 파일을 섞지 않는다. 충돌 해결로 제출 내용이 달라졌으면 recover/재검증/재제출하며 기존 테스트를 재사용하지 않는다.

통합 target에서 `begin-evidence`와 `record-evidence`에 `--stage integration`을 사용해 필수 AC를 다시 검증한다. `integrate`, 이어서 `close`가 실제 target 파일 hash·commit·spec/contract·AC·handoff를 확인한다. 둘 다 session=rfa-coordinator, submission generation, 최신 revision이 필요하다. arbitrary done/update는 없다. 구현·worker 검증·대상 branch 반영·제품 최종 flow 성공은 별도다.

`audit-control`은 OPS-000/001 같은 project에 지정된 개발 운영 작업만 canonical root에서 검증·완료 처리한다. 제품 task/worker의 integration 우회가 아니다. 기존 완료 이관 외의 not_required에는 이 경로와 이유·실제 evidence가 필요하다.

공유 계약 변경은 docs/CONTRACT_CHANGELOG.md 절차와 `publish-contract`로 처리한다. schema/fixture 확인 evidence와 새 file digest·영향 목록·호환성·migration 이유를 남긴다. 영향 task를 draft/stale로 만들고 coordinator의 `edit-spec --patch <명세 필드만 YAML> --reason ...`으로 새 baseline을 수락해야 한다. spec 변경은 spec_revision도 증가, heartbeat/owner는 revision만 증가한다. 관련 소스/계약 변경으로 완료 근거가 달라지면 다음 조회에서 verifying/stale로 기록하고 영향 AC만 재검증한다.

## snapshot·장애·범위 한계

짧은 `flock` 안에서 최신 원본 재조회→revision/claim/충돌 확인→task atomic replace→view 생성한다. 긴 테스트/구현 동안 lock을 잡지 않는다. 각 파일 rename은 원자적이나 여러 파일이 DB transaction인 것은 아니다. 원본 저장 뒤 view 실패 시 source_saved=true / repair_required를 반환하고 원본을 거짓 rollback하지 않는다. `refresh`로 복구한다.

snapshot은 source revision 집합 digest·schema·생성 시각을 포함하며 unchanged refresh는 byte-identical하다. validate는 stale/tampered view를 거절하고 status/list/ready는 원본 기준으로 복구한다. claim/완료 판단은 cached state가 아니다. 시작·새 claim 전·계약 변경·통합 전에 명시적으로 status를 다시 조회한다.

이 구현은 같은 호스트의 공유 local filesystem용이다. 원격/격리 세션이나 flock/atomic replace 보장 없는 network FS에서는 자동 claim을 켜지 않는다. coordinator가 직렬 상태 명령을 처리하고 code patch만 전달받는다. 새로운 서버/분산 DB/agent framework는 없다.

Codex 새 chat/worktree 또는 제공된 subagent의 별도 context 생성 기능은 사용할 수 있으나 모든 지침 전달 범위는 유지한다. [공식 worktree 안내](https://developers.openai.com/codex/app/worktrees), [subagent 안내](https://developers.openai.com/codex/multi-agent)를 참고한다. 현재 환경은 새 worker 생성 기능이 있지만 filesystem은 공유되므로 worktree를 따로 배정해야 한다. 사용자가 수동 새 세션을 여는 복사형 프롬프트도 `.agent/prompts/`에 제공한다. 존재하지 않는 agent 실행 CLI나 compact만으로 fresh context가 되었다는 주장은 하지 않는다.
