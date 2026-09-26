# P0-015 — post-WAL no-code revalidation

Goal/AC1–4: reverify server-owned authenticated sessions/history/runs, N:M Task links, restricted legacy migration and cross-owner non-disclosure after P0-016 changed local.py startup. Existing implementation is present; stale evidence does not mean missing implementation.

Facts: P0-016 commit4bac516 merged into canonical53d7068246b91a25dc9286acf02923ee0713cd8b, then fresh canonical tests31+164 passed and task closed. WAL activation now happens only during bounded path-locked initialization, not every connection. No session/auth schema change. P0-014 verified contract remains done; P0-005/P0-008 historical foundations unchanged. Prior stable-main-worker-01 and stable-main-target-01 P0-015 evidence remain preserved, not reused as fresh results.

Boundaries: server-generated run/session/thread IDs, direct-loopback-only keyless identity without forwarding headers, owner-scoped idempotency and internal projection; not production multi-user identity or real runtime/approval. Earlier sessions-01 projection, sessions-02 owner-key, sessions-03 HTTP existence-oracle failures remain in immutable history, followed by corrected sessions-http-01. No .env or provider call.

Inspection: feature task/P0-015 was clean and ff-only updated4f808b0→53d7068. uv sync --locked checked55packages, ignored inherited different VIRTUAL_ENV as documented; root environment unchanged. No running task process, pending external effect or dirty source. Explicit integrated_revalidation only; no new source commit required.

Fresh worker result: post-wal-worker-01 V1 tests/test_sessions.py 26 passed in1.47s, 11 existing durability warnings; supplemental tests/test_api.py 4 passed in0.19s, 2 same warnings. Zero skips/failures/errors. Both enforced timeout120. No product source edit or new commit. Evidence .agent/evidence/P0-015/post-wal-worker-01/result.json.

Next first action: record/submit unchanged source, then independently capture/run post-wal-target-01 on canonical53d7068 and integrate/close only after pass. Target tests for this attempt remain not_run at submission. Current canonical task.yaml integration/result/latest_evidence_file is authoritative; this handoff cannot preclaim future target success. Root will append actual target facts to WORK_LOG after the batch.

## Post-settings baseline715db79 revalidation
Actual clean feature ff-only53d7068→715db79; exact default lock sync complete. No dirty source, running task process, unintegrated change or external side effect. Main configuration changes invalidate prior proof conservatively without removing implemented ownership/session functionality. Existing worker Python3.12.13/SQLite3.50.4 retained; independent canonical target is Python3.12.13/SQLite3.53.1 with NAT1.8.0. No .env/provider access.

Next: capture worker source and rerun existing V1 plus API regression; unchanged-source submit; separately capture/execute target checks before close. Prior result files and actual failures remain preserved. Product E2E/real identity/approval/runtime remain outside this revalidation.

Fresh worker result: V1 26 passed in1.45s (11 known durability warnings); API4 passed0.18s (2 same warnings), zero skips/failures/errors. Both bounded120sec. Evidence .agent/evidence/P0-015/post-settings-worker-01/result.json. Next target execution remains not_run at submission.

