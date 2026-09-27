# P1-011 — 결재 인박스 UI PoC 대응 /v1/inbox 계약과 reference 서버

- 목표 / AC1~3: UI 시안(A · 메일형 3단 4화면 + 관리자 에이전트/샌드박스)의 표시 항목을 계약으로 정의하고 OpenAPI로 제공, PoC 데이터 reference 서버, 승희 Review→RequestDetail 매퍼.
- 구현 사실: src/rfa_mas/inbox/contract.py(Pydantic, extra=forbid), reference.py(FastAPI, in-memory, 21 operation, 모두 x-rfa-authority: rfa_module.review/runtime.sandbox/core), __main__.py(`python -m rfa_mas.inbox --port 8793`, loopback 전용), rfa_module.py(review_to_request/review_preview/to_review_call). fixtures/inbox/poc_cases.json(요청 7건·알림·활동·규칙·에이전트 5·샌드박스 4·게이트웨이 2·미리보기), fixtures/inbox/rfa_module_reviews.json(승희 Review 표본 6종, 고정 review.openapi.yaml로 schema 검사). docs/api/inbox.openapi.{json,yaml}(scripts/export_openapi.py 생성), docs/INBOX_API.md(화면→route/필드 표, 전이표, authority 대응).
- 결정: 결정 API는 action(respond/decline/hold/answer_question_only/block_author)+option_id+save_as_rule+idempotency_key. 종료 상태 재결정 409 invalid_state, 카드 불일치 409 stale_approval, 없는 옵션 422. 승희 review 매핑은 reviewed만 needs_approval, 카드 kind review_body(게시본 그대로/원본 초안까지; block은 옵션 없음). UI 결정→approve/reject 변환은 to_review_call. 3단계 공개 범위 실제 반영은 승희 Step 12+ 재작성 루프 이후(계약은 option_id/disclosure_level로 준비).
- 변경 파일: 위 + tests/test_inbox_reference.py, tests/helpers/openapi_check.py 및 fixtures/contracts/rfa_module/*(P1-010과 동일 내용 중복 추가). worktree rfa_mas_worktrees/P1-011-inbox, branch feat/P1-011-inbox-contract, commit ced00b8(base efa063a).
- 검증: tests/test_inbox_reference.py 8 passed(화면 항목, 결정 전이·규칙·알림·멱등, 관리자 토글/압축/설정/적용, 승희 표본 schema·매핑, OpenAPI authority·생성물 최신). ruff 통과. 실제 승희/다영 서비스 호출 없음.
- 실패/blocker: fixture span 오프셋·rule 패턴(underscore)·null final_body 수정 3건. 없음 pending.
- integration: main 이동(cc86868). P1-010 통합 뒤 새 worktree에서 cherry-pick(동일 파일은 동일 내용) → evidence → submit → merge → integrate/close.
- 다음 행동: tc.py intworker P1-011 <session> tasks/P1-011/handoff.md ced00b8. 이후 다영 UI가 reference로 화면 제작, 승희 review 어댑터(P1-008A gate)에서 rfa_module.py 사용.
