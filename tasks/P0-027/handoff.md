# P0-027 — installed NAT compatibility spike

Goal / AC1–AC4: retain core pins, verify actual release registration and custom-state mapping, isolate default installation, document unsupported boundaries.

Implemented only pyproject.toml, uv.lock, docs/NAT_COMPATIBILITY.md, tests/spikes/test_nat_compatibility.py in assigned feature worktree. Optional nat extra pins official nvidia-nat-langchain 1.8.0; every previous lock version unchanged. Default environment still excludes NAT; no new backend/server or graph rewrite.

Actual evidence: .agent/evidence/P0-027/nat-spike-01/source.json and result.json. Python 3.12.13, LangGraph 1.2.12, checkpoint 4.2.0, NAT core/langchain/eval/opentelemetry 1.8.0. Required spike: 8 passed in 0.68s, no skips. Fresh default-only /tmp/rfa-nat-core-3Ewmed/.venv: nat absent, 2 core isolation tests passed in 0.06s. Network blocked inside synthetic tests. uv lock --check, ruff, git diff --check passed.

Decisions: reuse official released wrapper externally. It needs messages, serializes custom DTOs to mappings, calls factories with empty RunnableConfig. A real checkpointer without thread ID is rejected. Outer P0-028 adapter must reconstruct trusted DTOs and invoke WorkService once; never bypass authentication/state/approval. Fake model usage is unknown, not zero. Streaming/HITL/tool profiler/exporter not verified; no live NVIDIA or full product E2E claim.

Failures retained: upstream guessed langgraph_wrapper.py 404 corrected using release tree; initial lint 3 ASYNC240 + E501 corrected by module-level path; inherited uv environment warning ignored root venv as expected. Supplemental full feature-worktree pytest: 9 failed, 255 passed in 28.21s, all nine migration tests use Store(ROOT) and correctly reject copied control root. Do not bypass marker or claim full suite pass. No repeated attempt without changed evidence. Coordinator notified; canonical-root integration regression required.

Next exact action: coordinator review feature commit scope and immutable evidence, merge without control copies, sync optional nat environment for integration, re-run eight-test spike and canonical full regression. Then P0-028 uses the compatibility table and trusted outer WorkService mapping; KB/evaluation tasks do not depend on NAT success.
