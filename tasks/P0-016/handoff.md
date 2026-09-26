# P0-016 scope reissue — unverified WIP

Objective: persistent Supervisor review wait/resume with current owner/policy checks; AC1–4 are not yet verified.

Source WIP d50de92d8a98be2992f1473cc7124da539a509be in original P0-016 worktree is preserved and clean. Worker stopped all tools/tests. Official sqlite saver3.1.1 resolved without changing existing pins; adds aiosqlite0.22.1/sqlite-vec0.1.9. Strict serializer, lifespan, Runtime.context, per-thread guard, wakeup-based ResponsePort query, seed-once marker and initial tests are implemented but unverified. Only formatting/diff checks run; evidence cycle0.

Coordinator inspected the required test compatibility change: tests/test_supervisor_boundaries.py directly builds SupervisorDependencies and used principal in graph state. Its helper must inject fresh runtime context and policy validator while preserving all negative assertions. Approved one-file scope addition requires a new fenced claim. Existing source/old claim history are not discarded.

Next: new clean P0-016-r2 worktree at current main, valid claim with expanded scope, cherry-pick preserved WIP, update direct graph helper, finish implementation and independent review before begin-evidence. Then actual fresh-process resume/current ACL/cross-company BU/credential canary/concurrency/state-reset checks, associated legacy regression, commit and submit. Final tool crash-window exactly-once and full E2E remain P0-021/P0-026. No real provider/sandbox/publication claim.

Error log: one apply_patch request had an extraneous context line and was rejected atomically. Removed that context and applied once successfully; no partial source mutation. No blind repeated product test retries.
