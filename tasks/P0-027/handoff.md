# P0-027 — post-teams no-code compatibility revalidation

AC1–4: current installed release/pins and actual wrapper8/default-only2 checks on frozen85d747ee4c837566aa8d2cdc54878ee580ebfe69. Historical spike/release-source review and all prior failure/success evidence remain preserved; this is not a new NAT integration or live model result.

Clean task/P0-027 FF confirmed. Exact uv sync --locked --group dev --extra nat --python <approved durable3.12.13> recreated this feature environment with168 packages, unchanged lock; Python3.12.13/SQLite3.53.1. Old environment retained under worktree .local/retained-venv-post-teams-20260926, no deletion/shared Python replacement. Default-only P018 environment is independently prepared and will explicitly use current027/src via PYTHONPATH. Original fresh mktemp install proof remains historical, not claimed to have occurred again now.

Manual review keeps NAT1.8.0 official release/installed metadata and wrapper source separate from product capabilities. Wrapper requires message shape, serializes DTOs to mappings, receives empty factory config and does not forward checkpoint thread config. WorkService/auth/checkpoint stay authoritative; callback/internal-tool tracing, streaming/HITL and provider tokens are not proven by this spike. No raw dotenv/config.env or network.

Worker post-teams-worker-01 actual command window11:40:30.092056Z–11:40:45.461156Z: metadata/source manual V1 reviewed, exact offline lock check169, declared installed V2 eight passed0.54s; independent actual noNAT current027/src check two passed0.13s. No skip/failure/retry. Both Python3.12.13/SQLite3.53.1; ordinary plugin loading. Additional manual source inspection confirmed wrapper lines60/79/113/115/118/244 limitations and unchanged pyproject/lock.

Next: submit unchanged source, then fresh target capture/manual review/installed8/noNAT2 rerun before close. No skip/empty run can replace installed smoke; KB does not depend on NAT. Task original owns later target status.
