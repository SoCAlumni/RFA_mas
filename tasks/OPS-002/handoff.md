# OPS-002 current-effort follow-up

The historical recovery/control implementation and successful generation6 schedule evidence remain in Git/approaches. The idle task/OPS-002-r2 worktree is clean; there are no pending task processes or external effects. Only the old integrated reservation is being archived to update a now-obsolete numeric schedule AC before a fresh scoped claim. No source is discarded.

Actual new failure: `.agent/evidence/P0-019/full-regression-01.json`, 596 passed/1 failed, D3 expected11 versus actual12. P1-006's approved estimate became2h. Subsequent P0-020 source preflight explicitly changed1.5→3h, so current sums are D1=8.5/D2=10/D3=12/D4=8 plus TECH6, serial36.5; original31.5 and intermediate34 are history, not current values. Product/security/final gates remain unchanged.

Exact next step: coordinator edit-spec from `.agent/input/OPS-002-current-effort-plan.yaml`, preserve failure/log and commit canonical baseline, fast-forward the clean feature, then claim and edit only `tests/test_task_migration.py` and `docs/TASK_REVALIDATION.md`. Run declared V1/V2 with actual canonical TASK_CONTROL_ROOT and isolated synthetic environment, capture immutable new evidence, submit for target verification. Do not change taskctl/framework or lower gates. Maximum3 log-based attempts for this observed failure; no blind retry or deletion of prior failures.
