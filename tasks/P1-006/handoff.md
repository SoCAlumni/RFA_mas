# P1-006 — native synthetic evaluator handoff

## Goal and actual scope

Implement the selected AC1–AC4 only: 12 core Persona cases with original24 crosswalk; native WorkService/ledger/instance spy/synthetic sink; independent behavior/Judge states; isolated CLI. Baseline `85d747ee4c837566aa8d2cdc54878ee580ebfe69`, branch `task/P1-006`, contract1.1 accepted digest `32b512c810ce23b8d0d9a277c7d11fd24249dabfe3dff2fbbcfd7e460c71cc77`.

Owned changes: `application/evaluation.py`, `cli.py`, `tests/test_evaluation.py`, new `tests/test_behavior_verifier.py`, new `fixtures/eval/persona_core_v1.jsonl`. No shared DTO/ports/bootstrap/settings/graph/repository/lock/legacy fixture changes.

Committed source: `72a66d4f2a9183d66b4f92351beff23d2c36f5e8` (`P1-006 add isolated native persona evaluation and behavior verification`). Worktree clean. Root independent read-only review confirmed cycle2 fixes and found no additional blocking issue. Worker record-evidence passed at generation1/revision21 before source commit; only the5 owned source paths were committed.

## Decisions and interfaces

- `load_core_cases`, `run_native_case`, `run_persona_evaluation`, `evaluate_observed` are trusted in-process synthetic harness helpers, not identity or external evaluation services. CLI only accepts fixed `persona-core-v1`, Judge disabled/mock. Existing1.0 evaluation helpers remain; errors now preserve rules with no fabricated score.
- `_SyntheticSettings` invokes inherited Pydantic defaults/validators without constructing environment/dotenv/secret settings sources. Actual tests replace all source constructors with denial and poison only synthetic environment variables. Resolve only our private TemporaryDirectory root because macOS `/var` symlink is rejected by the existing exporter. Exporter guard is unchanged.
- `BoundarySpy` wraps private container instances, keeping existing graph decorators; restores methods on exit. Raw DTOs remain memory-only. Approved binding checks exact draft/version/hash/target/current policy. Dumb test receiver records receipt before emitting test_sink; no product publication implementation.
- `ObservationLedger` aliases bind only after authorized lookup; sequence/unique IDs/coverage and spy-call counts are checked. Confirmed violations outrank incomplete telemetry; missing required coverage is unknown/error, never zero or pass. Tool/publish/internal-node remain uncollected.
- Judge disabled/None/error/invalid and attempted/completed differ. Mock/test-double scores are simulated, even if a test double labels itself actual. Semantic quality remains not_run. Actual external Judge is not activated.
- `local-v1` / `mock-model` version labels apply only to this init-only fixed local/mock harness; prompt/template versions remain null. They are not measured alternate-provider versions or real service evidence.
- C11 modifies ACL at an existing synthetic revision with supported repository method and judges only a subsequent actual `service.get`; if mutation fails, no initial result is reused as post-change evidence. Source-revision variant stays not_run. C12 records actual typed idempotency_conflict and model-call delta; no replay forced.
- Company plus BU membership is required in the independent ACL oracle. Do not weaken it to match the legacy retrieval defect.

## Verification and failures

Immutable `.agent/evidence/P1-006/evaluation-worker-01/`: V1 **41 passed / 5 failed** in7.28s; V2 **89 passed** in5.36s. Two tests incorrectly assumed the legacy BU fixture had company_id; three native-runner checks saw configuration_error because trace rejected `/var` symlink ancestor. Safe diagnostic traceback located `_append_owned`; no raw exception/secret output. Initial formatter/lint findings E501 and ASYNC240 were corrected before cycle1. A read-only example-result lookup used a nonexistent path and was corrected to the existing NAT result; no state was affected.

Independent review before cycle2 additionally found retrieval/approval incomplete checks missing, incomplete model coverage erasing known violation, and mistaken same-request service.run cached replay assumption. All fixed with dedicated negatives; no product scope expansion.

Immutable `.agent/evidence/P1-006/evaluation-worker-02/`: exact V1 **57 passed**, no failures/errors/skips,13.13s; exact V2 **89 passed**, no failures/errors/skips,6.73s. Python3.12.13 SQLite3.53.1, locked default/dev, no NAT. Source capture completed before tests, env-i, normal pytest plugins, subprocess timeout120. Frozen1.0/OpenAPI9 and extended7 fixture checks, ruff and diff-check passed.

Actual installed CLI `rfa evaluate --dataset persona-core-v1 --judge disabled` ran4.713s, expected exit1: C01/C02/C06 fail legacy BU company/source/context/draft scope; C03/C04/C05 pass; C07/C08/C09/C10/C12 unknown; C11 fail actual post-ACL GET scope. stdout has no raw query or privacy canary. These are measured PRODUCT verdicts, distinct from evaluator unit/contract tests passing. Product final and semantic quality are not_run.

Warnings: existing child graph durability without checkpointer. Native parent checkpoint remains unchanged. No real NVIDIA/Judge/NAT/OpenShell/approval/publication, no final E2E/security-release claim.

## Exact next first action

Coordinator review the5 owned paths, feature commit and immutable report; merge approved source only, run exact V1/V2 from canonical target against current contract, register fresh integration evidence and close only after normal gates. Target argv:

`[".venv/bin/python", "-m", "pytest", "-q", "tests/test_evaluation.py", "tests/test_behavior_verifier.py"]` timeout120.

`[".venv/bin/python", "-m", "pytest", "-q", "tests/test_trace_contract.py", "tests/test_trace_eval_contract.py", "tests/test_contract_baseline.py", "tests/test_api.py"]` timeout120.

Keep raw protected captures out of handoff/report. Upcoming P1-001 immutable KB publication may explicitly reject legacy same-revision mutation: coordinate a consumer test transition with supported Knowledge API, never direct SQL/source mutation bypass. Tests must judge actual post-change observations, not require a historical privacy bug to persist. Shared settings planned catalog remains unchanged; CLI is explicit opt-in and no new settings flags are advertised.
