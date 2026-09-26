# P0-018 — post-settings no-code revalidation

Goal/AC1–3: revalidate the existing approved-template selector at frozen main715db79142da1456a2fb1aae208d107d35b53a76 after P0-017 supported settings/bootstrap changes. No source edits, new product feature or contract publication. Original selector commit6973cfe and prior selector/stable-main evidence and integration remain historical and preserved.

Inspection: task had no claim and historical integrated/stale reservation. Reviewed stable-main-target-01 result/source plus submission hashes and ancestor commit. No task implementation/test process found. Feature task/P0-018 was clean at4f808b0; advanced ff-only to715db79, then confirmed clean. Owned selector files and contracts/INTEGRATION are unchanged between these revisions. Root source/HEAD/WORK_LOG stay frozen; only task-control updates are authorized. Feature interpreter remains Python3.12.13/SQLite3.50.4; target uses patched SQLite3.53.1. No environment changes or actual .env reads.

Interfaces remain unchanged: server-pinned immutable TemplateRegistry; authenticated identity and server grants/runtime/budget intersection; deterministic Benchmark/Research, no Engineering/unapproved version selection. Returned approved template remains separate from execution_budget; P0-019 must enforce it and refresh current grants. Hashes/DTOs are not authorization proofs.

Worker post-settings-worker-01 actually passed 2026-09-26T09:38:40–09:38:43Z: V1 selector53 (0.07s); supplemental contracts64 (0.61s); frozen v1.0 schemas/OpenAPI and9 fixtures valid; extended7 fixtures valid. No skips/failures/errors, source clean. Evidence: .agent/evidence/P0-018/post-settings-worker-01/result.json. No source commit was needed because source is unchanged.

Next first action: submit unchanged source, independently capture post-settings-target-01 and rerun exact V1 plus contract checks on canonical715db79; integrate/close only after target pass. Target revalidation is still not_run.

Limits: local pure selector and schema compatibility only. Not team provisioning, GPU/runtime execution, real NVIDIA/NAT/OpenShell, SQLite WAL concurrency or full RFA E2E. Historical failure/fix evidence is preserved; tests will not be replaced by old pass counts.
