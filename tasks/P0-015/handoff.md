# P0-015 — stable-main no-code revalidation

Goal/AC1–4: verify server-owned authenticated sessions/history/runs, N:M Task links, restricted legacy migration and cross-owner non-disclosure on main4f808b0 after durable resume integration. No product code is changed. P0-014 now has fresh worker and target evidence; P0-005/P0-008 historical foundation scope is unchanged.

Current facts: original sessions implementation and HTTP hardening are already integrated. Server-generated run/session/thread IDs, loopback-only keyless identity without forwarded headers, owner-scoped idempotency and internal graph projection remain the tested boundaries. This does not claim multi-user production auth, real runtime or external approval authority. P0-016 now owns checkpoint/resume and is the next separate revalidation.

History: sessions-01 failed 1.1 projection; sessions-02 automatic pass was superseded by owner-key finding; sessions-03 failed HTTP existence-oracle review despite passing tests. Corrected HTTP approach passed sessions-http-01 and was subsequently integrated. Original commits/evidence remain preserved; their former pending handoff text is historical, not current implementation absence.

Plan: exact V1 tests/test_sessions.py (120s) plus relevant tests/test_api.py regression; use isolated synthetic fixtures/no .env. Capture worker evidence then execute tests independently in canonical target. No-change submission and new target evidence are required before close.

Worker batch results: V1 26 passed in 1.34s (11 existing no-checkpointer durability warnings); supplemental API 4 passed in 0.21s (2 same warnings). New evidence is .agent/evidence/P0-015/stable-main-worker-01/result.json. After submit, consult canonical task.yaml integration/result/latest_evidence_file for the authoritative newest target state; a submit-time handoff cannot preclaim future target verification. Root records final target outcome in WORK_LOG after the batch.

Next first action: explicit integrated recovery from clean main4f808b0, capture stable-main-worker-01, run V1/API regression, submit unchanged source, then capture/run stable-main-target-01 and integrate/close only on actual pass.
