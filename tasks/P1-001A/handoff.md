# P1-001A — 권한 선필터·최신 lexical 검색·무효화

## 현재 구현 사실 (source worktree rfa_mas_worktrees/P1-001A, base 47f002f)

- 신규 `application/source_access.py`(ACL-only 판정, trusted project resolver 기본 empty, BoundAccess descriptor), `adapters/retrieval.py`(LocalRetrieval=local-lexical-v1 simulated=false, BoundContextReader: process-local 단기 policy receipt, L0 본문 0회/L2 선택 재검증), migration5 `kb_revision_context`(raw hash·부모 refs), trusted derived-write, SourceMetadata/SourceRead 1.1 DTO, KnowledgeAcl/KnowledgeDocumentV11.project_id(선택).
- `RETRIEVER_BACKEND` 기본값 local. mock은 명시 시뮬레이션 fixture(1.0 reference specimen 생성기, NAT 합성 평가, test_graph 실패 시나리오, 평가기의 non-success 시나리오)로만 사용.
- ResumePolicy는 read_sources(현재 head/ACL/부모 closure)로 재판정. WorkService.present_result/present_session과 API work/run/session GET이 현재 불가 draft/review를 통째로 제거하고 안전한 policy_denied/resume_review_required만 반환. 저장된 draft_json/hash/승인은 불변.
- 평가기: synthetic_settings 기본 local, non-success 시나리오만 mock. C11은 KnowledgeService 새 private revision 뒤 실제 GET을 관측하고 안전한 제한 응답을 pass로 판정.

## 이번 세션 오류와 수정 (cycle 1)

- 첫 전체 실행: 34 failed/683 passed. 원인: (1) 테스트 fixture 오류(KnowledgeExport domain_id·github row 형식 누락, 없는 get_draft 호출, 1.0 모델에 project_id 전달), (2) mock 전용 시나리오가 새 local 기본값에서 실행됨, (3) raw SQL로 기존 revision 본문을 바꾼 test_resume fixture가 이제 변조로 탐지됨(정상 fail-closed), (4) outward 재판정의 실제 policy 관측이 stop 뒤에 추가되어 trace 테스트의 "마지막 행" 가정이 깨짐, (5) extended 계약 재생성 필요, (6) test_task_migration은 worktree의 복사 control root라 원래 실패(제품 범위 아님).
- 수정: fixture를 제품 KB write 경로로 교체, mock 시나리오를 명시 설정, trace 테스트는 stop 행을 찾고 이후 행이 policy 재판정뿐임을 확인, write-extended/check/check-extended 통과, C11 판정을 명세대로 수정.
- 수정 후 제품 테스트(taskctl/task_migration 제외) 606 passed. rfa evaluate: C01–C05 pass, C06–C12 unknown(미구현 tool/publication/approval gate), C11 acl/past_result pass, exit 2.

## 결정·제한

- NAT adapter 제품 코드는 변경하지 않았다(합성 평가는 mock retriever만 허용). local reader를 NAT 경로 성공으로 집계하지 않는다.
- 실제 recipient/cloud egress·project identity provider·OS sandbox는 구현/검증하지 않았다(P1-005, P1-008 이후).

## 다음 행동

- spec16(scope에 scripts/contract_baseline.py, tests/test_trace_contract.py, tests/test_nat_smoke.py 추가) 후 generation3 재claim, commit 66b2d49.
- worker evidence retrieval-worker-03: V1 84 passed, V2 348 passed(fail/skip/deselect 0, env -i 격리).
- 다음: submit → main 병합 → RFA-EXTENDED 1.1 새 digest 발행 → integration evidence → integrate/close → consumer(P0-017/P0-018/P0-028/P1-006) 계약 수락·재검증.
