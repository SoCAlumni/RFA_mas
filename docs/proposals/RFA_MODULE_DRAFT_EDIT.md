# 제안: 결재 서버에서 초안을 고쳐서 게시 (RFA_module 결재 서버 v0.2.0 → v0.3.0)

- 보내는 쪽: rfa_mas (결재함 프런트 API, `docs/FE_APPROVAL_API_PLAN.md`, 결정 D-22)
- 받는 쪽: RFA_module 결재 서버 (`contracts/approvals.openapi.yaml`)

## 왜

시안의 초안 카드는 사람이 초안을 바로 고쳐 쓰고 「바로 응답」한다(「바로 고쳐 쓸 수 있어요」, 「수정함 · N자」). 지금 결재 서버는 `approve` 에 본문이 없고 저장된 `draft` 를 그대로 게시하므로, rfa_mas 는 고친 본문을 409 `draft_edit_unsupported` 로 막고 "재생성 요청으로 고쳐 달라"고 안내하고 있다.

## 제안하는 계약 변경 (하위 호환)

`POST /approvals/{id}/approve` 에 선택 본문:

```json
{ "draft": "사람이 고친 최종 본문" }
```

- 본문이 없거나 `draft` 가 비어 있으면 지금과 같다(저장된 초안 게시).
- 있으면 `pending` 에서만 받는다. 게시 전에 `draft` 를 바꾸고, 원래 초안을 이벤트로 남긴다: `{who: "human", what: "approved", detail: "edited"}` + 새 필드 `edits: [{draft: <원래 초안>, at}]`(선택).
- `approved`(게시 실패 후 재시도) 상태에서 본문이 오면 409 — 고친 본문은 한 번만 확정.
- 게시할 본문 검증은 지금 채널 규칙 그대로(GitHub 마커 붙이기, Slack 스레드 답글).

## rfa_mas 쪽 변경

`POST /inbox/{itemId}/respond {draft}` 가 저장된 초안과 다를 때 409 대신 위 본문을 그대로 전달한다. 고친 본문도 게시 전에 한 번 더 등급 검사(사외/사내)를 거칠지는 따로 정한다(추천: 거친다 — 사람이 고친 글에 사내 정보가 들어갈 수 있다).
