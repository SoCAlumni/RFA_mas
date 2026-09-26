# 교체 가능한 기본 로컬 모듈

2026-09-26 사용자가 팀원 모듈이 없으므로 기본 구현을 먼저 만들도록 승인했다.
기존 팀원의 최종 서비스 책임은 유지한다. 아래는 구현할 repository-local reference
계약이며 각 task의 검증/통합 전에는 사용 가능한 기능이 아니다.

## 경계

- P1-008C: 수동 검토/승인과 모의 게시 영수증, 제한된 READ tool HTTP 서비스.
  별도 SQLite DB가 **이 local stand-in에서만** 승인 원본이다. core DB/checkpoint를
  열지 않는다. 실제 승희 서비스가 연결되면 그 서비스가 승인/게시 원본이다.
- P1-008D: TeamSpec lifecycle과 합성 handler만 실행하는 local runtime HTTP 서비스.
  자체 journal을 사용한다. OS sandbox/OpenShell/MCP 프로토콜 구현이 아니다.
- P0-025A: 현재 core API와 위 검토 모듈을 사용하는 작은 동일 출처 UI. 실제
  팀원 UI/서비스로 교체 가능하다. 미구현 일정/역할 실행/실제 게시를 가능하다고
  표시하지 않는다. 최종 조립은 공통 bootstrap/DTO 소유 task와 직렬 관리한다.
- P1-008: 기존 consumer/mapper 및 runtime capability 조립. 실제 서비스 gate인
  P1-008A/B, OpenShell P1-007B, 최종 10 E2E는 이 stand-in 성공과 별개다.

## 작은 로컬 안전 경계

서비스 factory는 별도 DB 경로, 서버에서 검증한 설치 owner, `SecretStr` 서비스
token, 허용 origin을 명시 주입받는다. token/owner를 JSON body의 주장으로
대체하지 않는다. CLI에서 비밀을 출력하거나 브라우저/URL/localStorage에 제공하지
않는다. 기본 제품 mock 실행에 이 서비스나 사용자 API key를 필수화하지 않는다.

loopback 연결, 정확한 Host/port allowlist, 브라우저의 동일 Origin 검사를 함께 적용한다.
Forwarded 헤더와 외부 origin, 헤더가 없는 browser 변경 요청은 거절한다. 서비스간
요청은 인증 헤더가 필요하며 browser Origin이 있으면 반드시 같은 origin이어야 한다.
웹 UI는 동일 출처 요청과 CSRF 방어를 적용하고 service token은 서버에만 둔다.
요청 validation/error/access log에 본문·token·private canary를 반사하지 않는다.
이는 같은 PC의 개발용 경계이며 적대적인 OS 계정이나 네트워크 sandbox를 대신하지 않는다.

## Response/Tool 준비 계약 — P1-008C

- 기존 `DraftBundle`/`ReviewDecision` 1.0은 `/v1/reviews` submit/get 호환을 유지한다.
  submit은 자동 승인하지 않고 pending이다. 고정된 설치 owner의 인증만 허용한다.
- 1.1 `DraftBundleV11`, `ApprovalReference`, `PublicationReceipt`는 기존 Pydantic
  원본을 재사용한다. 모듈 전용 요청 envelope만 local_response.py에 둔다.
  `/v1/local/reviews`에 1.1 제출, `/v1/local/reviews/{draft_id}` 조회,
  `/v1/local/reviews/{draft_id}/decision`에 승인/거절/수정 요청을 보낸다.
- 결정 요청은 최신 version/payload hash와 바인딩한다. 승인자 ID는 인증된 서버
  owner에서 얻는다. body가 부여한 승인/권한은 신뢰하지 않는다. 더 높은 버전 제출은
  이전 승인 무효화, 동일 버전 다른 내용/다른 idempotency payload는 conflict다.
- `/v1/local/publications`는 승인 ID를 원본 DB에서 조회하고 정확한 payload와
  유효기간을 재검증해 **합성 local-artifact 영수증만** 만든다. 보호된 외부 write는
  하지 않는다. 현재 source/정책 재검증은 core P1-005A/P1-008의 별도 gate다.
  이 endpoint 결과를 실제 공유 허가/게시 성공으로 해석하지 않는다.
- 모의 outcome_unknown은 영속 저장하고 GET 조회로 유지한다. 새 POST/재시작으로
  재게시하거나 성공을 추정하지 않는다. 실제 게시 네트워크 client는 없다.
- `/v1/tools/execute`는 문서화한 무부작용 합성 READ allowlist만 제공한다.
  shell/URL/file-path 및 WRITE는 실행하지 않는다. HTTP ToolPort이지 MCP가 아니다.
- 건강 상태는 local/mock, supported contract versions/capabilities를 노출한다.
  입력/결정/게시 멱등성은 DB transaction과 unique key로 처리한다.

## Runtime 준비 계약 — P1-008D

`create_local_runtime_app`은 위 인증 helper와 별도 store를 사용한다.
TeamSpec prepare/cleanup, AgentSpec+TaskRequest run/status/cancel을 제공하고
정확한 route/envelope는 해당 모듈의 생성 OpenAPI로 확인한다. 기존 RuntimePort를
재사용하고 API 이름만으로 실제 core HTTP capability가 준비됐다고 선언하지 않는다.
TeamSpec owner/domain/member ID와 서버의 허용 역할/tool을 확인한다. 임의 코드,
shell, URL, caller 선택 handler는 실행하지 않는다. 지원하지 않는 OpenShell과
capability는 명시 오류다. 합성 결과에는 simulated=true가 붙는다. 재시작 시
in-flight는 unknown/recovery로 남기고 자동 replay하지 않는다.

## UI 준비 계약 — P0-025A

`src/rfa_mas/ui/`의 별도 ASGI factory/static assets로 세션 목록/생성, 노트 입력,
질의/결과, 현재 검토 상태와 수동 검토 경로를 제공한다. 새 frontend framework나
관측 서버가 필요하지 않다. body를 HTML로 주입하지 않고 텍스트로 렌더링한다.
고정 core/service 주입만 허용하고 사용자가 URL을 지정하는 proxy는 만들지 않는다.
기존 API 조립을 복제하거나 core graph에 URL/auth/UI를 넣지 않는다.

추가 작업 추정은 2h+2h+2h이며 기존 4일 범위에 숨겨 넣지 않는다. 현재 사용자
추가 승인 범위로 추적한다. 이번 문서 자체는 구현·보안·실제 팀원 검증 증거가 아니다.
