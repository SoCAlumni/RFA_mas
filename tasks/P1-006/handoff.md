# P1-006 — post-knowledge no-code evaluator revalidation

Original72a66d4 implementation and evaluation-worker-01 failure/corrected02/target evidence remain unchanged. Current frozen76b2186 and accepted extended KB70f3416 reference supersede historical source85/contract32b512 for new validation, not old evidence.

Clean task/P1-006 ff-only current main, locked default/dev offline55 packages, Python3.12.13 SQLite3.53.1 and NAT absent. Previous integrated c7f7e4f binding/target ancestry and current014/006D prerequisites inspected. No old process/WIP/pending external side effect. Root unrelated.gitignore preserved. No source changes or tests weakened.

Evaluation acceptance is NOT12 product scenario success. Keep original24-ID crosswalk, trusted fixed identities/native ledger+spies, test_sink distinct, incomplete unknown vs observed failure, Judge simulated/disabled/error and semantic/final not_run. New immutable KB may reject C11 legacy same-revision ACL mutation: retain actual error/unknown/not_run and never use the earlier result as post-change evidence or bypass storage checks. Source revision variant remains not_run. Historical BU/source failures remain facts of their original execution only.

Worker post-knowledge-worker-01 actual12:40:53.637520Z–12:41:21.410900Z: V1 57 passed16.98s (49 existing warnings), V2 89 passed6.18s (20 existing warnings); no skip/failure/error/retry. Source capture completed first, env-i/normal plugins/timeout120.

Actual installed CLI expected exit1: C01/C02/C06 fail; C03/C04/C05 pass; C07/C08/C09/C10/C12 unknown; C11 error. C11 past_result_scope remains unknown; ACL/past_result/source_revision variants all not_run after immutable KB rejects old same-revision mutation. Earlier response is NOT reported as post-change exposure. Canary absent; product_final and semantic not_run. This differs honestly from pre-KB C11 measured failure; no source/test change made.

Next first action: submit unchanged artifact and capture/repeat target exact V1/V2 + safe CLI before integrate/close. Actual task.yaml/evidence owns later results. No real NVIDIA/Judge/NAT/runtime/publication/security-release/E2E claim. A supported Knowledge API mutation fixture transition requires a separately scoped future change, not a storage bypass.

## 재검증 post-team (2026-09-26T15:43Z)

- 사유: P0-020 integrated 86d770f (shared contracts/local/service) and OPS-003 tool fix; RFA-EXTENDED 1.1 888c3d6d
- 소스 변경 없이 현재 통합 HEAD 11aedac에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 post-ops005 (2026-09-26T16:37Z)

- 사유: OPS-005 changed TASK_EXECUTION_RULES.md (global context ref) at 4bf0ec9; direct dependencies (transitive) of next claims only
- 소스 변경 없이 현재 통합 HEAD ab9cf68에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-004B (2026-09-26T16:51Z)

- 사유: Direct/transitive dependencies of P1-004B before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 0bdef6a에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P2-003 (2026-09-26T17:01Z)

- 사유: Direct/transitive dependencies of P2-003 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD d3cad28에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-005 (2026-09-26T17:13Z)

- 사유: Direct/transitive dependencies of P1-005 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 0161cc2에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-004 (2026-09-26T17:24Z)

- 사유: Direct/transitive dependencies of P1-004 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD cf05f47에서 계획된 검증을 worker/target 단계로 재실행한다.


## 재검증 보수 — P1-005 이후 staged-context 관측 (2026-09-27)

- P1-005 통합 뒤 pre-P1-004 재검증에서 V1이 4 failed(53 passed)였다. `test_behavior_verifier`의 retrieval 관측이 unknown이 되거나 KeyError가 났다.
- 원인: 공개/owner local 대상의 근거 로딩이 retrieval port가 아닌 staged-context loader로 이동했는데, BoundarySpy는 retrieval port만 감쌌다.
- 수정(이 task 소유 파일):
  - `evaluation.BoundarySpy`가 P1-005C의 `container.context.load`를 retrieval로 관측한다. 실제로 서비스했거나 실패한 호출만 센다(None은 port spy에 맡김).
  - 회귀 테스트 `test_staged_context_boundary_is_the_observed_retrieval_boundary` 추가.
- 평가 규칙(불일치=ERROR, 누락=UNKNOWN)은 바꾸지 않았다.



## 재통합 — P1-005 이후 staged-context 관측 보수 (2026-09-27)

- 원래 통합 이력(재검증 포함)은 attempts.approaches에 보존. P1-005 이후 재검증 실패로 discard하고 이 변경으로 다시 통합한다.
- scope 추가(edit-spec): `src/rfa_mas/bootstrap.py`, `tests/test_staged_context_boundary.py`. 등록했던 P1-005C는 P1-001A→P1-006 의존으로 순환이 생겨 이 task가 흡수한다.
- 변경: bootstrap이 staged-context load를 `container.context`(StagedContextBoundary)로 노출하고 domain graph가 호출 시점에 조회한다. BoundarySpy가 그 경계를 retrieval로 관측한다.
- 개발 검증: test_evaluation + test_behavior_verifier + test_staged_context_boundary 61 passed. late lookup을 early binding으로 바꾸면 경계 테스트 2건이 실패함을 확인했다.

