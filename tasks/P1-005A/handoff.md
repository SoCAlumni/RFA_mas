# P1-005A — DRAFT 편집·승인 무효화·안전한 mock 게시 상태

## 목표와 AC

사용자가 고친 초안을 낡은 승인으로 보내는 사고를 막는다.

- AC1 본문·첨부·대상·권한·source/policy 변화 시 기존 승인 무효화와 재검토
- AC2 검증된 승인 없이 publication succeeded 전이 불가
- AC3 현재 version/hash/target/정책/근거와 맞지 않는 결정 거절
- AC4 run 상태와 publication 상태 분리, simulated receipt와 실제 영수증 구분

## 구현 사실 (wip/P1-005A 26f172c, c11577a)

- `application/drafts.py` DraftLifecycle
  - `state`: 최신 버전, 버전 목록, 승인 mirror, `approval_valid`/`invalid_reason`, 게시 receipt. source/정책이 바뀌면 draft·review를 숨긴다.
  - `edit`: expected_version CAS. 항상 새 불변 버전을 만든다. 비owner 대상의 비공개 표식은 거절한다. 대상 변경은 ResumePolicy로 source별 공유를 재승인한다.
  - `request_review`: 현재 버전만 검토 서비스에 제출한다(owned 멱등 key).
  - `publish`: 유효 승인만 허용한다. durable PENDING receipt를 기록한 뒤 publisher를 호출한다. ack 유실은 OUTCOME_UNKNOWN(next_action=query)으로 두고 조회로만 대사하며 재게시하지 않는다. 확정 거절은 FAILED(review)다.
  - `query`: PENDING 중 crash는 OUTCOME_UNKNOWN으로 기록하고 조회 결과와 binding을 대조한다.
- 승인 판정 사유: `draft_changed`, `approval_binding_mismatch`, `policy_changed`, `sources_changed`, `review_<decision>`, `review_missing`, `content_hash_mismatch`.
- `adapters/mock.py` MockPublisher
  - in-memory sink, 같은 key의 다른 binding은 conflict.
  - `lose_ack`/`fail` key로 모의한다.
  - mode=mock, `local-artifact:*` 참조, 네트워크·외부 write 0회.
- `state_machine.PUBLICATION_TRANSITIONS`는 Run 전이표와 분리했다. OUTCOME_UNKNOWN→PENDING 전이는 없다.
- `local.py` migration8
  - `draft_version_meta`(첨부 manifest), `publications`(owner·idempotency unique, Run당 1개).
  - `draft_versions`/`append_draft_version`(CAS)/`get_publication`/`put_publication`(expected_status CAS).
- contracts: `DraftEditRequest`, `PublishRequest`, `DraftState`(additive 1.1).
- API
  - `GET /v1/runs/{id}/draft`, `POST .../draft/edits`(201), `POST .../draft/review`.
  - `POST|GET /v1/runs/{id}/publication`.
  - 409 매핑: approval_required, publication_exists, approval_binding_mismatch, resume_review_required.
- 배선
  - bootstrap은 response_backend=mock일 때만 MockPublisher를 주입한다. 다른 backend면 publisher가 없어 501을 반환하며 mock으로 fallback하지 않는다.
  - observations SAFE_ERROR_CODES에 approval_required, publication_exists를 추가했다.

## 검증 (개발)

- tests/test_draft_lifecycle.py 15 passed
  - 승인 게시 1회와 replay, 다른 key 거절, 게시 뒤 편집 금지
  - 편집 후 v1 승인 replay 거절, 위조 hash/target 거절, 재검토 후 v2만 게시
  - 첨부/대상 변경
  - 비공개 표식, owner 근거의 공개 대상 변경 거절
  - source revision·정책 변경 시 무효화와 숨김
  - pending/revision/rejected 결정은 게시 불가
  - ack 유실 후 조회 대사(재게시 0회), PENDING crash 뒤 unknown
  - 확정 거절, 재시작 보존, 타인 404, publisher 미구성 501
  - HTTP 경로·오류 매핑
- tests/test_state_machine.py 76 passed(게시 전이표 추가)

## 한계

- 승인 원본은 검토 서비스(mock/stand-in)다. 이 mirror는 권한을 만들지 않는다.
- mock 승인은 in-memory라 재시작 뒤 approval_valid=false다. 영속 승인 원본은 P1-008C stand-in 또는 실제 서비스.
- HTTP stand-in 게시 연결과 callback은 P1-008. 실제 게시·영수증은 not_run.
- publish 경계 trace는 uncollected로 둔다.

## 다음 행동

P1-005B(피드백), P0-021(effect ledger)가 이 게시 기록을 재사용한다.

## 재검증 pre-P0-021 (2026-09-26T18:08Z)

- 사유: Direct/transitive dependencies of P0-021 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 2223a7f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-008 (2026-09-26T18:24Z)

- 사유: Direct/transitive dependencies of P1-008 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3c5cb6f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-022 (2026-09-26T18:39Z)

- 사유: Direct/transitive dependencies of P0-022 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3466bc5에서 계획된 검증을 worker/target 단계로 재실행한다.
