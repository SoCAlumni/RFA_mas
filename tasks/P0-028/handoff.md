# P0-028 — actual NAT evaluation adapter

Worker worker-nat, generation1; baseline35f43400c57641dea9ab8397b018a98d7f5bea8b. Own only nat_eval.py, configs/nat/rfa_eval.yml, configs/nat/rfa_eval_cases.json, tests/test_nat_adapter.py, tests/test_nat_smoke.py. Source commit fd6bc94, five owned files only; feature worktree clean. Coordinator read-only final review found no submission blocker. Worker passed is not target-integrated/done.

## Implemented (not yet fully verified)

- Explicit trusted synthetic Python API with ready local/mock container modes, fixed case ID, no existing run replay, cloned request/principal held outside NAT in ContextVar. Caller owns lifecycle; WorkService and its checkpoint/auth/approval remain authoritative.
- Installed NAT1.8 EvaluationRun + local evaluator registration via public load_config discovery; messages carry safe durable ObservationLedger only. No CLI/.env/export callbacks/raw output/profiler/real model. Whole outer execution LangSmith context guard. No added dependencies/shared source.
- Missing/errors/interrupted evaluator/results cannot pass; provider token unknown/null and uncollected/incomplete tool/publish/internal coverage are retained. Deterministic rule score is not semantic Judge quality.
- Root reviewed stale-run concern: pre-existing owner-visible run rejected, no cached observation proof reused or tool replay.

## Actual evidence / errors

nat-adapter-01 source/result preserved under canonical .agent/evidence/P0-028. V1:21 passed/1failed/1teardown error/16warnings in14.46s. V2 actual installed2 passed/3warnings in1.23s. Failed V1 pending checkpoint assertion used incorrect ResumeRequest(run_id); source schema requires event_id. Ambient positive-control created actual LangSmith client whose background /info request was blocked by socket test guard: one attempted outbound, zero successful network. No credential/private data was supplied. Adapter runs otherwise passed; this is not a completed security gate.

Corrections before cycle2: actual ResumeRequest(event_id), real SDK CallbackManager enablement with synthetic BaseCallbackHandler tracer factory and no client/background worker, cache clear before/after, physical network guard retained. Native guard not modified or mocked. Initial lint1 longline, later2unused imports+1longline, laterimport order corrected with formatter/ruff. Read-only path probes for guessed upstream evaluator/register.py/dataset_handler.py and tests/test_trace.py returned missing; actual installed source paths used. No blind product test retries.

## Next first action / limits

Cycle2 exact V1:22 passed/15warnings in3.56s, timeout120; V2 actual installed2 passed/3warnings in1.27s, timeout300. Independent actual no-NAT P0-014venv with current P0-028/src PYTHONPATH:3 passed/1warning0.53s. Narrow contract/trace DTO/resume95 passed/17warnings5.88s, timeout120. No skips/errors/outbound attempts. Frozen9/extended7 baseline fixtures, targetedruff and git diff --check passed. Existing nested graph durability-without-checkpointer warning and upstream ast.Str deprecation are recorded, not suppressed.

Cycle2 worker source/result is immutable at canonical `.agent/evidence/P0-028/nat-adapter-02/`; failed01 remains preserved. Next first action after verifying submission: root reviews commit fd6bc94 and manually merges, captures independent target source and runs exact `.venv/bin/python -m pytest -q tests/test_nat_adapter.py` (timeout120) and `.venv/bin/python -m pytest -q tests/test_nat_smoke.py` (timeout300), with env-i/normal plugins, then narrow contract/resume checks and baseline checks before integrate/close. No automatic merge occurred. Three correction cycles maximum. Real NVIDIA/Judge, streaming/HITL resume support through NAT, TeamFactory, OpenShell and finalE2E remain unverified/out of scope. NAT existing enable_nat setting remains reserved; invoke explicit module function only.

Official reference used: NVIDIA NeMo-Agent-Toolkit v1.8.0 packages/nvidia_nat_eval/src/nat/plugins/eval/runtime/evaluate.py and docs/source/improve-workflows/evaluate.md. Installed files establish actual APIs; exceptions swallowed at _run_single_legacy_evaluator require explicit missing-result checks. No upstream implementation copied into core.
