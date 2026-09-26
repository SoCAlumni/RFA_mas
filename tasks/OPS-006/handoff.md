# OPS-006 — 만료된 integrated revalidation claim 재발급

- 원인(사실): pre-P0-024 재검증에서 P0-014 worker 검증이 venv 미동기화로 실패했고, 이어서 chain이 멈춰 lease가 만료됐다(18:57Z). 만료 claim은 heartbeat 불가(Recovery required)다. 일반 recovery는 무변경 재검증이라 제출 범위 검사에서 실패하고, discard는 통합 이력을 잃는다.
- 구현: recover에서 미제출 integrated_revalidation claim(마지막 approach가 같은 generation)을 integrated_revalidation inspection으로 재발급한다. baseline 확인은 그 approach에 기록된 통합 이력으로 revalidation_baseline을 다시 사용한다. 새 approach는 같은 이력을 이어받는다.
- 검증(개발): 새 테스트 3개(재발급과 이력 유지·이전 generation fencing·재close, 일반/제출 claim 거절, 범위 밖 제출 거절). 재발급을 끄면 2건 실패함을 확인했다.
- 한계: 제품 기능 아님.

