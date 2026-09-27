# P1-007 — NemoClaw 지원 경로·교육 연결 설계

- 산출물(wip/stack 9e9ba67; nemoclaw_research 작성): docs/evidence/nemoclaw.md, EDUCATION_MAPPING 교육 목표 5개 매핑, INTEGRATION 호출 경계 절.
- 공식 확인(2026-09-27)
  - OpenShell 최신 stable은 v0.1.1이며 macOS Apple Silicon을 지원한다(VM driver).
  - NemoClaw는 alpha v0.0.129이고 공식 agent는 OpenClaw, Hermes, LangChain Deep Agents Code뿐이다. 다른 harness는 Unsupported다.
  - MCP는 HTTPS Streamable HTTP만 받으며 loopback은 거절한다.
- 설계: 우리 FastAPI/LangGraph backend는 sandbox 밖에서 동작하며, 지원 agent가 sandbox 안에서 최소 API를 호출한다(/healthz, /v1/sessions, /v1/sessions/{id}/work, /v1/runs/{id}). 보호되지 않는 범위(.env, SQLite, checkpoint, trace, backend egress)를 명시했다.
- NemoClaw 설치·실행은 not_run이다(P1-007A blocked).

