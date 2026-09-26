# P1-006C — Langfuse OTLP export, redaction, 보존 확인

## 구현 (wip/stack 135b202, 3e23621, 0648b40; langfuse_worker 개발)

- `adapters/langfuse.py`: OTLP/HTTP JSON으로 `/api/public/otel/v1/traces`에 전송한다. `x-langfuse-ingestion-version: 4`를 붙인다.
- 로컬 trace를 먼저 기록하고 그것을 원본으로 둔다. export는 추가 동작이다.
- 보내는 속성은 allowlist(opaque ID·코드·지속시간·호출 수·버전 참조)뿐이다. 질의·excerpt·제목·원문은 보내지 않는다.
- 전송 조건은 셋 다 필요하다: `TRACE_BACKEND=langfuse`, loopback `LANGFUSE_BASE_URL`, `LANGFUSE_EXPORT_ENABLED=true`. 키만으로는 권한이 생기지 않는다.
- 비밀 값이 들어간 body는 전송하지 않는다.
- 결과는 exported/failed/not_attempted로 따로 기록한다. 승인된 2xx만 exported이며 재시도하지 않는다. export 실패는 run 실패가 아니다.
- 보고되지 않은 token은 보내지 않는다(0으로 채우지 않음).

## 검증

- 개발: test_security와 test_settings(fake OTLP 서버)로 연결 오류·timeout·401·500·비 JSON 2xx·부분 거절·legacy 207이 모두 failed임을 확인했다. 비밀과 canary가 전송되지 않는다.
- live(real, 로컬 Langfuse 4.46.0 공식 docker-compose, 2026-09-26 18:16–18:17Z, langfuse_worker)
  - 14/14 관측을 ID로 되읽었고 canary·질의·초안·raw ID·키가 저장되지 않았다.
  - 포트를 닫으면 14/14가 failed(connection_error)이고 run은 완료된다.
  - `DELETE /api/public/traces/{id}`는 200이었고 10–51s 뒤 조회에서 사라졌다. MinIO 원본 업로드는 남았다.
  - 보존: OSS는 Enterprise data-retention 없이 `LANGFUSE_INIT_PROJECT_RETENTION`을 무시한다(DB retention_days NULL). P1-006F(blocked)로 분리했고 만료는 검증하지 않았다.

## 한계

- 원격(비 loopback) Langfuse는 미구현이며 거절한다.

