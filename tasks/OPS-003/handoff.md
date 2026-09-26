# OPS-003 — 통합 evidence head race 보정

- 원인: integration record-evidence가 기록 시점 target HEAD를 result.head로 저장했다. P1-001 post-retrieval-target에서 다른 세션 commit(caf7bd2)이 검증 중 들어와 manifest head(3a1a5ee)와 달라졌고, revalidation_baseline이 모든 중첩 예약의 recover를 거절했다.
- 수정(58a355d): result.head = begin-evidence manifest head. 과거 기록은 manifest head의 후손이고 fingerprint·파일 hash·spec·contract가 같을 때만 허용.
- 테스트: tests/test_taskctl.py에 동시 commit race 바인딩·revalidation 수락, legacy 후손 허용/비후손 거절 3건 추가.
- cycle1(fix-worker-01): 기존 회귀 test_reserved_contract_change_requires_historical_binding[manifest_head] 2건 실패 — ancestry만으로 변조된 manifest head(HEAD^)를 허용했다. 951c9cb에서 캡처 head의 blob hash가 manifest의 clean 파일 hash와 모두 같을 때만 허용하도록 보강. 실제 P1-001 기록은 보강 후에도 guard 통과를 확인했다.
- 다음: worker/target evidence → 통합 → post-team 재검증 cascade 재개.


## 2026-09-27 stale 예약 정리(discard)

- OPS-004 통합 뒤 stale 예약(task-control)이 OPS-005 claim을 막고, 의존 task OPS-002가 todo여서 재검증도 불가한 순환이 확인됨.
- 활성 claim·미통합 제출 없음. 통합된 소스(58a355d, 951c9cb)는 main에 그대로 있다. 이력은 attempts.approaches에 보존.
- OPS-005 통합 뒤 재검증 절차 문서를 갱신하는 실제 변경으로 다시 완료한다.
