# P0-028 — post-teams historical revalidation

AC1–4: existing trusted synthetic NAT1.8 EvaluationRun adapter on frozen85d747ee4c837566aa8d2cdc54878ee580ebfe69. Current authoritative accepted contract is additive RFA-EXTENDED1.1 digest32b512; V1 old prose mentions the preceding37bec7 baseline, preserved as historical context rather than overriding current contract_refs. No new implementation or claim that optional enable_nat settings/CLI is active.

Clean task/P0-028 ff-only from fd6bc94 to current main, exact nat+dev lock sync. Python3.12.13/SQLite3.53.1 and actual NAT1.8.0. Historical integration guard passed; all014/016/027/006D direct dependencies now independently verified done. No prior process, dirty source or pending side effects. Existing nat-adapter-01 failure (ResumeRequest field, blocked LangSmith positive-control network attempt) and corrected02/target success remain immutable; no failure is erased.

Preserved scope: ready local/mock container, fixed synthetic case, native authenticated WorkService once, pre-existing-run rejection, authorized DB ledger, metadata-only NAT config/results, outer tracing guard and owned empty callbacks. Real model/tool/publication, semantic Judge quality, internal-node coverage, streaming/HITL resume through NAT and full RFA E2E are not claimed.

Worker actual11:43:29.818465Z–11:43:38.995354Z: V1 adapter22 passed3.90s (15 warnings), V2 actual EvaluationRun/local evaluator2 passed1.10s (3 warnings), separate actual noNAT/current028 source adapter2 passed0.48s (1 warning), frozen9/extended7 valid. The actual noNAT count is2 for this exact selected file; no previous report count was reused. Metadata confirms NAT core/langchain/eval1.8.0 and core pins. No skips/errors/retries. Normal plugins, explicit120/300s timeouts and PATH-only env-i; no actual dotenv/credentials/network.

Next: submit unchanged artifact and independently capture/run post-teams-target-01 before integrate/close. Task original owns later target state; prior nat-adapter-01 failure remains preserved.
