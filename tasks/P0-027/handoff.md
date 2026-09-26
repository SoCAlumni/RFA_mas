# P0-027 post-knowledge no-code revalidation

Frozen target76b2186eb09035a46d10b1d652acd61b712b6b01, feature task/P0-027 clean fast-forward from85d747e. Historical implementation, release source and all failure/success evidence preserved; recover only historical integrated state, not a new integration claim.

Actual offline `uv sync --locked --offline --group dev --extra nat`: resolved169/checked168 packages, lock unchanged. Python3.12.13 SQLite3.53.1, NAT core/langchain1.8.0. Root env unchanged. Initial metadata diagnostic incorrectly queried absent distribution name `nvidia-nat`; corrected using actual installed nvidia-nat-core/langchain names. This was a read-only metadata error, not missing installed NAT or a test retry. Inherited VIRTUAL_ENV mismatch warning ignored project source as documented.

Current selected scope remains optional wrapper installed8 plus actual no-NAT2, no live model, no source rewrite; WorkService/auth/checkpoint remain authoritative. Official v1.8.0 guide/source references and installed wrapper checked; factory empty config/message conversion/thread forwarding limitations remain. Streaming/HITL/internal tool coverage/token usage not proven. No raw env/config.env/network.

Next first action: fresh source capture, manual installed metadata/lock/source check and exact V2 installed8 plus default-only current-source2; fresh target capture/repeat then integrate/close only after valid evidence. No existing result reuse. Canonical .gitignore is unrelated user change and untouched.

## 재검증 post-retrieval (2026-09-26T14:25Z)

- 사유: P1-001A integrated 66b2d49 changed shared KB/retrieval/service/contract sources and published RFA-EXTENDED 1.1 f711bab8
- 소스 변경 없이 현재 통합 HEAD 3a1a5ee에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 post-team (2026-09-26T15:08Z)

- 사유: P0-020 integrated 86d770f changed shared contracts/local/service sources and published RFA-EXTENDED 1.1 888c3d6d
- 소스 변경 없이 현재 통합 HEAD 975baae에서 계획된 검증을 worker/target 단계로 재실행한다.
