# P0-014 — stable-main no-code revalidation

Goal/AC1–4: reverify existing additive 1.1 DTO/port/fixture and frozen 1.0 compatibility on main 4f808b0 after integrated session, resume and selector changes. No new contract or product feature is implemented by this batch. Prior implementation and failed/successful evidence remain in approaches and immutable evidence files.

Facts: prior target evidence post-sessions-integrated-01 was on an older source and is stale, not absent implementation. Source task/P0-014 was inspected clean, then ff-only updated to main 4f808b0. Canonical contract artifact is RFA-EXTENDED1.1 digest 0c285d7389dd2cd21676a887aae810e6a34b979c0b4b3a96f6831c8137fd8a65; provider role remains unchanged. Existing prerequisite P0-003/P0-010 handoffs retain their historical/local foundation scope.

Decision: explicit integrated_revalidation with new worker and target evidence, no-change submission. Actual .env is untouched, no remote service called, no Git commit or product-source edit. Existing feature venv lacked the newly committed SQLite checkpointer dependency, so synchronize only the existing lock before executing tests.

Worker verification on fixed main4f808b0: V1 tests/test_contracts.py 14 passed in 0.01s; V2 tests/test_trace_eval_contract.py 50 passed in 0.59s, both below timeout120. Evidence: .agent/evidence/P0-014/stable-main-worker-01/result.json. Prior tests were not reused. Product workflow/NAT/cloud/OpenShell/real approval evidence is outside this schema verification. Target verification is recorded separately after submission.

Next first action: recover at current clean main baseline; capture new source evidence; execute V1/V2 in feature, record and no-change submit; execute both again on fixed canonical target before integrate/close. Keep root informed before starting P0-015.
