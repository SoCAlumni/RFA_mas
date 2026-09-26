# OPS-002 — current approved effort regression maintenance

Goal: preserve all existing protocol, migration, security and final acceptance gates while aligning only the numeric schedule regression with current approved task estimates. Parent archived/discarded inactive historical reservation generation7, applied reviewed AC4/V2/spec patch and committed canonical85d747ee4c837566aa8d2cdc54878ee580ebfe69. Existing historical implementations, failures and successful attempts remain preserved.

Actual source preparation: idle clean OPS-002-r2 worktree at a8d288846e43c377cebd48fe18beb8147c150284 fast-forwarded to85d747ee; uv sync --locked succeeded, only editable package rebuilt. Existing feature Python3.12.13/SQLite3.50.4 retained and explicitly recorded for control-only tests; no shared environment upgrade and no product SQLite validation claim. Normal claim worker-control generation8/revision48 succeeded without scope bypass. Parent concurrently assigned non-overlapping product prerequisite revalidation; canonical HEAD/source remains parent-controlled.

Failure basis (cycle1): .agent/evidence/P0-019/full-regression-01.json records actual full ordered run597 collected/596 passed/1 failed, D3 old expectation11h versus current12h after approved P1-0061→2h. Trace-cache/NAT/team cases passed in that same run; whole suite still failed. No historical failure is overwritten or reset. New P0-0201.5→3h was then approved before this maintenance baseline. Locked canonical task/project sums were directly checked: D1=8.5/D2=10/D3=12/D4=8; TECH_D1=2.5/TECH_D2=2.5/TECH_D3=1 (total6); serial D1–D3+TECH=36.5; separate LLMOps3.5. Initial31.5h and intermediate34h remain historical facts.

Only source changes: tests/test_task_migration.py current per-day and serial assertions plus history comment; docs/TASK_REVALIDATION.md history/current sums/failed-evidence reference and limits. CLI/framework code, test_taskctl.py, root WORK_LOG and all functional/security/final-gate assertions unchanged. No actual dotenv/credentials/provider/network access.

Preparation diagnostics before canonical spec application: functions JS structuredClone unavailable, replaced by JSON clone; read_input returned text rather than parsed YAML, validator corrected to yaml.safe_load after checking function. Third preparation check passed Task schema and unchanged AC1/2/3/5/V1; these are tool preparation errors, not product-test reruns. No source changes were made before authorized claim.

Current corrected verification is cycle2 relative to the preserved full-suite failure, attempt current-effort-worker-02. Exact V1 tests/test_taskctl.py (timeout180) and V2 tests/test_task_migration.py (timeout120) run with env-i PATH and explicit canonical TASK_CONTROL_ROOT, normal pytest plugins. Protocol mutations use temporary fixture/control directories; migration audit reads canonical state. Ruff and git diff --check passed. Final counts/evidence are attached after both commands finish. Maximum3 meaningful cycles; no unchanged retry or lowered AC.

Actual corrected worker verification PASSED: V1 94 passed112.50s (timeout180), V2 17 passed6.23s (timeout120), no skips/errors/failures/xfails. Immutable .agent/evidence/OPS-002/current-effort-worker-02/{source,result}.json records unchanged-source capture and exact assertions/commands/output. Root full-regression-01 remains the first failure; this correction passed without a further retry.

Next first action: scoped feature commit, then submit verifying. Parent coordinates main merge after the existing product revalidation window; no worker merge, publication or done before independent target evidence. These are task-management tests, not product E2E/live technology/security certification.

## 재검증 post-retrieval (2026-09-26T14:10Z)

- 사유: P1-001A integrated 66b2d49 changed shared KB/retrieval/service/contract sources and published RFA-EXTENDED 1.1 f711bab8
- 소스 변경 없이 현재 통합 HEAD 62422a2에서 계획된 검증을 worker/target 단계로 재실행한다.
