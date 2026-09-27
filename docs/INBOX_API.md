# 결재 인박스 API (`/v1/inbox`) — UI PoC 대응 계약

원본 화면: 2026-09-27 사용자 제공 "결재 인박스 UI 시안"(채택안 A · 메일형 3단, 관리자 2화면).
계약 원본은 `src/rfa_mas/inbox/contract.py`(Pydantic)이고 `docs/api/inbox.openapi.yaml`/`.json`은 생성물이다.
갱신: `.venv/bin/python scripts/export_openapi.py inbox`. 손으로 고치지 않는다.

reference 서버(PoC 표본 데이터, in-memory):

```sh
uv run python -m rfa_mas.inbox --port 8793      # http://127.0.0.1:8793/docs
```

reference는 mode=reference·simulated=true다. 결재 승인·게시·샌드박스 제어를 실제로 수행하지 않는다.
UI(다영)는 이 서버로 화면을 만들고, 각 route의 `x-rfa-authority`가 가리키는 실제 원본 어댑터가 뒤에서 교체된다.

## 화면 → route / 필드

| 화면 요소 | route | 필드 |
| --- | --- | --- |
| 좌측 하단 "이다영 · 샌드박스 4개 실행 중" | `GET /v1/inbox/me` | `InboxUser.display_name/sandboxes_running` |
| 결재함 4 · 알림함 4 · 모든 인박스 4 | `GET /v1/inbox/counts` | `InboxCounts` |
| 인박스 행(GitHub 이슈 · public-desk · 공개 등급 · 2) | `GET /v1/inbox/inboxes` | `InboxSummary.name/agent_id/clearance/pending_count` |
| 요청 목록 탭(전체/결재 필요/자동응답)·검색 Ctrl K·정렬 | `GET /v1/inbox/requests?status=open&q=&sort=` | `RequestSummary.requester.initials/title/subtitle/status/received_at/is_new` |
| 상단 breadcrumb "GitHub 이슈 / team/rfa-test#34", "결재함에서 결재 12 보기" | `GET /v1/inbox/requests/{id}` | `RequestDetail.source.label`, `approval_id` |
| 원본 화면(읽기 전용 이슈/Slack DM/터미널) + "원본 열기" | 같은 route | `OriginalView.messages` 또는 `terminal`, `source.url`, `reply_placeholder` |
| 주입 공격 강조 + 경고 문구 | 같은 route | `OriginalMessage.flagged_spans`, `OriginalView.injection_warning` |
| 결재 카드(질문·항상 빼는 것·옵션 3개·추천·배지) | 같은 route | `DecisionCard.question/always_excluded/options[].recommended/badge` |
| "public 샌드박스가 막은 시도 3건" 표 | 같은 route (+ `GET /v1/inbox/activity?request_id=`) | `DecisionCard.blocked_attempts[]`, `ActivityEvent` |
| 버튼 "이 범위로 응답 / 응답하지 않기 / 보류 / 이대로 처리" | `POST /v1/inbox/requests/{id}/decision` | `DecisionSubmit.action/option_id`, 라벨은 `primary_label/secondary_label` |
| 체크박스 "비슷한 요청에 이 결정을 규칙으로 쓰기" | 같은 POST | `DecisionSubmit.save_as_rule` → `DecisionReceipt.rule` |
| "응답 전문 미리보기 / 답장 전문 미리보기" | `GET /v1/inbox/requests/{id}/preview?option_id=` | `ReplyPreview.body/excluded` |
| 하단 "샌드박스가 이 결재를 직접 승인하려다 차단됐습니다 · 활동에서 보기" | 상세 route | `RequestDetail.alerts[]` (`activity_id`로 활동 연결) |
| 알림함 | `GET /v1/inbox/notifications`, `POST .../{id}/read` | `Notification` |
| "결재 9의 규칙으로 자동응답" | `GET /v1/inbox/rules`, `DELETE .../{id}` | `Rule`, `RequestSummary.status=auto_replied` |
| 관리자 · 에이전트 목록/상세(컨텍스트 사용량·불러오는 자료·호출 통계·7일 그래프) | `GET /v1/inbox/admin/agents[/{id}]` | `AdminAgentDetail.context/sources/stats.last_7_days` |
| 자료 토글·대화 압축·기억 비우기 | `PUT .../sources/{source_id}`, `POST .../compact`, `POST .../clear-memory` | 다른 구획 자료는 409 `not_selectable` |
| 관리자 · 샌드박스 목록/상세(등급·인박스·제공자·게이트웨이·정책 스위치) | `GET /v1/inbox/admin/sandboxes[/{id}]` | `SandboxDetail.inference/gateway/policies` |
| "변경 적용 / 되돌리기" | `PATCH .../sandboxes/{id}`, `POST .../apply` | `pending_changes`, `ApplyResult.requires_recreate` |

## 결정(`DecisionSubmit`) 의미와 상태 전이

| 카드 kind | action | 결과 status | subtitle |
| --- | --- | --- | --- |
| disclosure_scope / share_scope | `respond`+option | `decided` | "결재 N · <옵션>로 응답" |
| disclosure_scope / share_scope | `decline` | `declined` | "응답하지 않기로 결정" / "답장하지 않기로 결정" |
| research_direction | `respond`+option | `decided` | "<옵션>로 진행" (다음 실행에 반영) |
| 모든 kind | `hold` | `held` | 다시 결정 가능 |
| blocked_handling | `respond`+`no-reply`/`decline` | `declined` | 기록만 남기고 닫음 |
| blocked_handling | `answer-question-only` | `in_progress` | 에이전트가 첫 문장만으로 공개 범위 결재를 다시 올림 |
| blocked_handling | `block-author` | `declined` | 작성자 차단 규칙 |

- 결제된 요청(`decided/declined/auto_replied/in_progress`)에 다시 결정하면 409 `invalid_state`, 카드가 바뀌었으면 409 `stale_approval`, 없는 옵션은 422 `invalid_option`.
- `idempotency_key`가 같으면 같은 receipt를 돌려주고, 같은 key에 다른 본문은 409.
- `DecisionReceipt.upstream.authority`가 실제 처리 주체를 알려 준다. reference는 항상 `reference/simulated=true`.

## 실제 원본과의 대응 (`x-rfa-authority`)

| authority | route | 실제 구현 위치 |
| --- | --- | --- |
| `rfa_module.review` | 요청 목록/상세/결정 | 승희 review 서비스 `GET /reviews`, `GET /reviews/{id}`, `POST /reviews/{id}/approve|reject` (호스트 loopback 전용). 매퍼: `src/rfa_mas/inbox/rfa_module.py` |
| `runtime.sandbox` | 활동 기록, 관리자 에이전트/샌드박스 | 다영 NemoClaw/OpenShell(게이트웨이 8080/8090, 정책 preset, agents yaml). 이 저장소는 표시 계약만 정의 |
| `core` | 미리보기, 규칙, 알림, 자료 토글, 결재자 정보, 리서치 방향(결재 13) | 이 저장소 core(API 8000). 리서치 방향은 팀 run 재개로 연결 |

승희 review와의 매핑 규칙(`rfa_module.py`):

- `reviewed`만 `needs_approval`이다. `opened..scanned`는 `in_progress`, `approved/posted`는 `decided`, `rejected`는 `declined`, `needs_human`은 `needs_human`.
- 결재 카드 kind는 `review_body`: 옵션은 "게시본 그대로"(추천, `final_body`)와 redact일 때 "원본 초안까지"(배지 "기밀 검토 전"). block이면 옵션이 없고 "재작성 요청/거절"만 남는다. `always_excluded`는 verdict의 remove/blur 규칙 id다.
- UI 결정 → review 호출: 게시본 승인 = `approve`; 다른 옵션·보류·거절 = `reject`(reason에 `scope:<option>`/`hold`/`decline`). PoC의 3단계 공개 범위(진행 사실만/방향까지/수치까지)는 승희의 `rejected → drafted` 재작성 루프(Step 12+)가 생겨야 실제로 반영된다. 계약의 `option_id/disclosure_level`은 그때도 그대로 쓴다.
- `approve/reject`는 샌드박스에서 호출할 수 없다(다영 `deny_rules` + 승희 loopback 검사). 인박스 백엔드는 호스트에서만 이 호출을 한다.

## 검증

```sh
uv run python -m pytest -q tests/test_inbox_reference.py
```

PoC 4개 화면·관리자 2개 화면의 표시 항목, 결정 전이·규칙·알림·멱등성, 승희 Review 표본(고정 review.openapi.yaml로 schema 검사)의 매핑, OpenAPI 생성물 최신 여부를 검사한다. 실제 팀원 서비스 호출은 포함하지 않는다.
