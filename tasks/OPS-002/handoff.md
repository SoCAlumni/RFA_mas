# OPS-002 — 계약 발행 후 통합 task 재검증 후속 보수

기존 recovery/일정 보수는 bd1e340으로 main 통합되고 worker 66+17, target 66+17 테스트가 통과했다. 기존 evidence와 통합 결과는 approaches에 보존한다. 현재 stale는 후속 WORK_LOG 변화로 생겼다.

새 조사: execute의 reconcile이 완료 consumer를 verifying/reserved로 바꾼 뒤 publish-contract가 모든 reservation을 거절해 새 계약 발행이 불가하다. edit-spec도 같은 예약을 무조건 거절해 현재 contract digest 수락을 못한다. active claim/미통합 제출 보호는 유지하면서 역사적 통합 증거가 있는 비활성 stale consumer의 발행/명시 계약 수락만 허용해야 한다. 관찰은 코드 검토이며 temporary protocol fixture 재현과 수정 검증이 다음 행동이다.

이전 source worktree clean/실행자 중단을 확인했다. 미통합 제품 변경이나 외부 부작용은 없다. 새 source=../rfa_mas_worktrees/OPS-002-r2, baseline a612fc3. 새 도구·framework를 만들지 않고 기존 cli/tests/TASK_REVALIDATION 문서 scope 안에서 고친다. 기존 AC는 유지하고 실제 old-token/미검증 close/dependency gate가 그대로인 음성 회귀를 추가한다. source를 고정한 뒤 evidence를 수집한다.
