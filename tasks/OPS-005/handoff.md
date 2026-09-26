# OPS-005 — stale 통합 예약 claim 차단 완화·target 이동 중 재검증 보정

- 목표/AC: AC1 활성 claim·미통합 제출이 없는 stale integrated 예약은 새 claim 충돌 사유가 아님(의존성 gate·활성/제출/blocked 예약은 유지), AC2 재검증 중 target 이동 시 clean FF만 무변경 제출 허용, AC3 기존 회귀 유지.
- 원인(사실): 계약 변경마다 모든 통합 task가 stale 예약을 가져 다음 claim 전에 전체 cascade가 필요했다(통합 수에 비례해 증가). OPS-002 재검증은 P1-008C 통합(a0a9347)으로 target이 이동해 submit이 거절되고, claim 중이라 revalidation recover도 불가했다.
- 구현: `store.inactive_stale_integrated()`와 readiness 충돌 제외, `cli._fast_forwarded_to_target()`와 submission_paths의 무변경 FF 분기. TASK_EXECUTION_RULES.md에 직접 의존성 재검증·최종 전체 재검증 gate를 명시.
- 운영 정리: 도구 공백으로 생긴 순환 때문에 OPS-002(in-flight 재검증)와 OPS-003(stale 예약)을 recover --disposition discard로 정리했다(이력 보존). 둘 다 이후 실제 절차 문서 변경으로 다시 완료한다.
- 검증: 새 테스트 3개(겹치는 stale 예약 비차단+의존성 차단, 활성/제출 예약 차단 유지, target 이동 FF 재검증) 통과. 각 보정을 되돌리면 해당 테스트가 실패함을 확인했다. 전체 test_taskctl는 worker/target evidence로 기록.
- 한계: 최종 acceptance 전 전체 재검증 1회는 여전히 필요하다. 제품 기능 검증이 아니다.

