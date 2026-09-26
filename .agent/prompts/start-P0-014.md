# 첫 실행 — P0-014 (OPS-000 baseline 이후)

이 저장소의 계약 coordinator로 P0-014만 수행한다. 이번 실행 범위는 해당 task의 계약·fixture·검증이며 다른 제품 기능/팀원 서비스·게시·배포는 시작하지 않는다.

```bash
export TASK_CONTROL_ROOT=/Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" --control-root "$TASK_CONTROL_ROOT" status
"$TASK_CONTROL_ROOT/.venv/bin/python" "$TASK_CONTROL_ROOT/scripts/taskctl.py" --control-root "$TASK_CONTROL_ROOT" context-pack P0-014
```

AGENTS.md → docs/PROJECT_CONTEXT.md → TASK_EXECUTION_RULES.md → 위 pack → tasks/P0-014/handoff.md → docs/EVALUATION_CONTEXT.md A → 지정 DTO/port만 읽는다. 전체 task를 주입하지 않는다.

현재 HEAD/등록 baseline이 없으면 OPS-000 해소 조건을 보고하고 claim/코드 실행은 중단한다. 자동 commit/stash하거나 control root를 feature source와 혼동하지 않는다. 승인된 clean baseline이 생기면 최신 show 결과의 revision으로 rfa-coordinator claim을 얻고 source worktree/branch를 기록한다. 실제 옵션은 taskctl claim --help로 확인한다.

기존 DTO를 원본으로 Execution/Evidence/Policy/Draft/Eval 의미·null·버전·소유권 및 7종 fixture를 구현한다. 별도 schema 원본, 가짜 미래 ID, trace 기반 인증을 만들지 않는다. 계획된 테스트는 구현 후 V1/V2의 실제 argv로 검증한다. skip/미수집은 passed가 아니다. .env/키 값은 읽지 않는다.

증거와 handoff를 기록해 verifying으로 제출한다. coordinator 통합/검증 후에만 done/publish-contract 및 consumer digest 수락을 처리한다. 완료 보고: 변경 파일, AC별 실제 결과/증거, 계약 영향, 지원하지 않는 경계, 다음 첫 행동. NAT/팀원 live 성공을 주장하지 않는다.
