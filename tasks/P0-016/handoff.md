# P0-016 — post-trace no-code revalidation

Goal/AC1–4: reverify the already integrated SQLite checkpoint/review-wakeup implementation after P1-006D observation migration2, guarded tracing, and outward-only error projection. Current frozen target/source is 35f43400c57641dea9ab8397b018a98d7f5bea8b. P0-015 post-trace target proof is done; P0-027 dependency-settings reservation is now released.

Facts: source P0-016-r2 was clean at715db79, ff-only35f4340 and exact default uv sync --locked succeeded. Both feature and root use Python3.12.13/SQLite3.53.1; root has NAT1.8.0, default feature does not. No source edit, pending task process, unintegrated change or external side effect. Old unpatched environment remains recoverably retained, not activated.

Historical proof/failures preserved: resume implementation3700de1; WAL startup fix4bac516 after stable-main-worker-01 actual locked-WAL failure; wal-startup-02/wal-integrated-02; post-settings-worker-01 and post-settings-target-01. Full old handoff is in canonical Git history; immutable attempts and approaches remain. This is new source revalidation, not proof reuse or retry reset.

Boundaries: fresh Runtime.context principal, current source/ACL/policy checks, owner-before-checkpoint, same-host thread lock, ResponsePort authority-only wakeup, seed-once and parent sync/child exit remain. No arbitrary write-crash exactly-once, archived-output revocation, final E2E or real provider/runtime claim. P1-006D full-suite cache-isolation failure is separately scoped to P0-019; no fullsuite in this batch.

Next first action: capture new worker evidence; execute declared V1 tests/test_resume.py with env-i/normal pytest plugins and timeout120, plus direct contract/API regression; no-change submit; independently repeat target checks, integrate/close only after valid evidence. Actual .env/keys untouched.

Worker actual post-trace result: V1 resume31 passed5.38s/17knownwarnings; narrow API/contract/baseline/trace DTO supplement81 passed1.76s/6knownwarnings. Zero skips/errors/failures with timeout120 and normal plugin loading. Evidence .agent/evidence/P0-016/post-trace-worker-01/result.json. No source commit needed. Next independent target remains not_run at submission.

