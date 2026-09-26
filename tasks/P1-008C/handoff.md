# P1-008C — 수동 승인·모의 게시·READ tool 로컬 대체 모듈

## 구현 사실 (wip/P1-008C 8de4cc6, 보조 worker local_modules_worker)

- reference/local_security.py(설치 owner·SecretStr token·정확한 Host/port·동일 Origin·Forwarded 거절, 본문/토큰 미반사), local_response_store.py(별도 SQLite, private 권한, core/타 서비스 DB 거절, unique key 멱등), local_response.py(1.0 /v1/reviews 제출은 pending, 1.1 /v1/local/reviews 수동 승인/거절/수정 요청을 최신 version·content hash·payload hash·target에 결합, 새 version은 이전 승인 무효화, /v1/local/publications은 유효 승인만 합성 local-artifact 영수증(mode mock, external_write_performed=false), outcome_unknown 영속·재게시 없음, /v1/tools/execute 무부작용 READ allowlist).
- 결정 route는 core graph가 제출하는 1.0 draft도 version+hash+target에 결합해 수동 승인할 수 있다. 모의 게시는 1.1만.
- 기존 ResponseHttpAdapter/ToolHttpAdapter를 ASGI로 연결해 pending→수동 승인→approved, 404→None, READ/deny, WRITE 미전송을 확인.

## 검증

- tests/test_local_response.py 24 passed(+test_http_contract 34). 실제 MCP/게시/팀원 서비스가 아니며 동일 OS 계정 내 개발 경계다.

## 다음

- bootstrap 조립·rfa local-stack(P1-008/P0-025)과 실제 승희 서비스 교체(P1-008A)는 별도.

