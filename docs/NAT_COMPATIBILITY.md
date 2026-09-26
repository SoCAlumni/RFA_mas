# P0-027 — optional NAT compatibility baseline

This is a compatibility spike, not the RFA NAT adapter (P0-028), a live NVIDIA
model result, or a claim that checkpoint/HITL and all internal tools are traced.
No core graph, authentication, state ownership, or service entrypoint changes.

## Release selection and reproducible installation

On 2026-09-26, the official **v1.8.0** release's `nvidia-nat-langchain` metadata
allows Python `>=3.11,<3.14` and LangGraph `>=1.0.5,<2.0.0`. Its wrapper is
`nat.plugins.langchain.langgraph_workflow`, registered as `langgraph_wrapper`.
Use the released wheel, not `develop`, a copied wrapper, or the entire blueprint.

The optional `nat` extra pins `nvidia-nat-langchain==1.8.0`. This is the smallest
published package containing this wrapper, but it is not a small dependency
tree: upstream includes multiple model integrations, eval and OpenTelemetry.
`uv.lock` now resolves 166 packages across default and optional selections. No
previously locked package version changed. Installing this extra added 113
third-party distributions plus the rebuilt editable RFA package; default sync
still installed 52 distributions including RFA/dev dependencies and no `nat`.
No backend, server, GPU runtime, or external API call is required for the spike.

| Actual installed component | Version |
| --- | --- |
| CPython | 3.12.13 |
| LangGraph / checkpoint | 1.2.12 / 4.2.0 |
| langchain-core | 1.6.5 |
| Pydantic / FastAPI | 2.13.5 / 0.141.1 |
| nvidia-nat-core / langchain / eval / opentelemetry | 1.8.0 each |

Reviewed installation commands, executed only in the P0-027 feature worktree:

```sh
uv pip install --python .venv/bin/python --dry-run 'nvidia-nat-langchain==1.8.0' \
  'langgraph==1.2.12' 'pydantic==2.13.5' 'fastapi==0.141.1' 'httpx==0.28.1' \
  'pydantic-settings==2.15.0' 'uvicorn==0.53.0'
uv lock
uv sync --locked --extra nat
uv lock --check
.venv/bin/python -m pytest -q tests/spikes/test_nat_compatibility.py
.venv/bin/nat eval --help
```

The resolver dry-run succeeded before editing dependencies; actual extra sync
succeeded. `nat eval --help` works and lists `--config_file`, `--dataset`,
`--result_json_path`, `--reps`; it is not an executed RFA evaluation config.
P0-028 must supply and verify the product adapter/evaluation path separately.

Default-only verification uses a fresh directory from `mktemp -d`:

```sh
# Replace the path with the actual mktemp output; do not reuse the main venv.
UV_PROJECT_ENVIRONMENT=/tmp/rfa-nat-core-3Ewmed/.venv uv sync --locked
/tmp/rfa-nat-core-3Ewmed/.venv/bin/python -m pytest -q tests/spikes/test_nat_compatibility.py
```

The default environment must have no `nat` import. Only the two core isolation
tests are collected there; that result is explicitly **not** an installed NAT
smoke. In the extra environment all eight tests must collect and pass, with no
skip. The authoritative observed counts/results/source manifest are recorded
under the task's attempt evidence, not inferred from these planned expectations.

## Observed boundary and P0-028 design

The spike uses the actual installed registration function and actual compiled
LangGraph with a deterministic `FakeListChatModel`. Both a compiled graph symbol
and a graph factory are tested. It compares native/wrapped outputs, identity
decision and one node invocation, without network or real credentials. Missing
extra selection raises explicit `NAT_UNAVAILABLE` in the **spike helper**; a
production readiness setting/adapter is still P0-028 work.

| Boundary | Support / limitation |
| --- | --- |
| Message input/output | Wrapper requires `messages`; a raw RFA DomainState is not sufficient. Extra fields are allowed but `model_dump()` turns DTO objects into mappings. Revalidate WorkRequest at the outer adapter; obtain principal from trusted fixture/session, never from a persona message. |
| Compiled graph / factory | Actual registration smoke covers both. The factory receives an empty `RunnableConfig`; it is not RFA's authenticated invocation config. |
| WorkService | P0-028 should wrap one non-interactive WorkService call in a message-shaped outer graph, keeping persistence, authorization and approval in RFA. Do not point NAT directly at a worker graph and bypass WorkService. |
| Run linkage | Explicit synthetic run ID survives the custom-state boundary. Product session/task/team/policy/draft joins need P0-028 and the extended contract. |
| Checkpoint / HITL | Wrapper calls graph.ainvoke without the application's thread config. A checkpointer graph with no thread ID is tested as an explicit error, not supported resume. Keep RFA's checkpointer and resume API authoritative. |
| Streaming | Upstream has `_astream`, but this spike does not verify RFA streaming or interactive resume. Unverified, outside P0 representative path. |
| Nodes / tool callbacks | Registered framework wrapper metadata exists. Actual profiler hook/exporter and internal RFA ToolPort interception are not verified here; registration alone proves neither. P0-028 must inspect observed events and call counts at exposed boundaries. |
| Usage | Fake model has no usage metadata. Record unknown/null, not zero. Upstream ChatResponse conversion creates a default Usage object; do not treat converter defaults as measured provider tokens. |
| Errors / traces | Upstream rethrows with original exception text. Production adapter must normalize/redact before exporting. Raw workflow/message/file exporters remain disabled until canary tests pass. No observation backend has been added. |
| Environment | Upstream supports loading `config.env`; RFA spike deliberately leaves it `None`. Never direct NAT to `.env` or copy secrets into its config. |

## Error log and retry handling

- Initial guessed upstream source URL `.../langgraph_wrapper.py` returned HTTP
  404. The v1.8.0 repository tree identified `langgraph_workflow.py`; its fetch
  succeeded. No blind retry or package downgrade.
- First local lint found three ASYNC240 path-resolution calls inside async tests
  and one long line. Hoisted the immutable test path to module initialization;
  the next lint passed. No AC was removed.
- `uv` warned that inherited root `VIRTUAL_ENV` differed from the feature/temp
  environment and explicitly ignored it. All installs targeted the documented
  feature or temporary path; root environment was not modified.
- Resolver/import/test failures must retain their actual command and version in
  task evidence. Three correction cycles maximum; repeated unsupported wrapper
  behavior is a P0-027/P0-028 blocker, not permission to rewrite core or block KB.

## Versioned primary references

- [NVIDIA v1.8.0 wrapper guide](https://github.com/NVIDIA/NeMo-Agent-Toolkit/blob/v1.8.0/docs/source/run-workflows/existing-agents/langgraph.md)
- [Released package metadata](https://github.com/NVIDIA/NeMo-Agent-Toolkit/blob/v1.8.0/packages/nvidia_nat_langchain/pyproject.toml)
- [Wrapper implementation](https://github.com/NVIDIA/NeMo-Agent-Toolkit/blob/v1.8.0/packages/nvidia_nat_langchain/src/nat/plugins/langchain/langgraph_workflow.py)
- [Upstream conversion tests](https://github.com/NVIDIA/NeMo-Agent-Toolkit/blob/v1.8.0/packages/nvidia_nat_langchain/tests/test_langgraph_workflow.py)

Upstream Apache-2.0 examples informed the integration shape; implementation code
was not copied into core. No NVIDIA live model, approval/publication service,
OpenShell, NemoClaw, or full RFA E2E gate is validated by this spike.
