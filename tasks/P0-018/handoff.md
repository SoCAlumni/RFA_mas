# P0-018 — post-teams no-code revalidation

Revalidate existing selector AC1–AC3 on frozen main85d747ee4c837566aa8d2cdc54878ee580ebfe69 and accepted RFA-EXTENDED1.1 digest32b512c810ce23b8d0d9a277c7d11fd24249dabfe3dff2fbbcfd7e460c71cc77. Historical selector implementation, previous failures and worker/target evidence remain preserved; no new product implementation.

Clean task/P0-018 was ff-only advanced to current main. Historical integration binding/target ancestry passed the existing inactive_integrated_guard. No previous selector process, dirty source, unintegrated changes or pending external side effects were found. Default exact lock environment is Python3.12.13/SQLite3.53.1; old venv is recoverably retained in this worktree .local/retained-venv-post-teams-20260926. No real .env, credentials or provider calls.

Worker post-teams-worker-01 actual window 2026-09-26T11:29:53.809505Z–11:30:00.999575Z: declared V1 selector53 passed0.04s; supplemental contract78 passed1.54s with4 existing durability warnings; frozen9/extended7 valid. No skips/errors/retries. Exact V1 assertions correspond to the pinned-template deterministic-selection and strict authorization/budget negative tests. All commands use bounded subprocess timeouts and minimal PATH-only environment with normal pytest plugins.

Next: submit unchanged source and capture independent post-teams-target-01, then repeat declared V1 plus bounded supplemental checks on frozen canonical target before integrate/close. TeamFactory, live model, sandbox and final RFA E2E success are outside this selector revalidation. Current authoritative later status is task.yaml.
