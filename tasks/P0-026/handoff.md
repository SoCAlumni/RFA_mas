# P0-026 — 현재 main 기반 최종 로컬 인수

- 목표/AC1~4: 합성 자료→권한 검색→팀 선택/재사용→근거/후보→대상별 초안→정확한 mock 승인/게시→예약/재시작 복구 및 10개 시나리오 gate 보고. 실제 팀원/모델/UI 성공으로 확대하지 않는다.
- 원래 WIP `ea76550`을 `93ae0a3` 위에서 회수했다. `7aadfa3`은 demo/fixtures/harness, `82b6919`는 retention import 보존·문서 참조/format, `72904f5`는 예산 fixture 보수다. bootstrap/공통 DTO/설정/정책 코드는 변경하지 않았다.
- 실제 실패: 첫 worker V1은 11 passed. V2는 54 passed/3 failed(같은 budget 변형의 3회 반복). `.agent/evidence/P0-026/completion-worker-0927014552/`에 실패 원본을 보존했다. 후속 V3~V5는 그 시도에서 미실행이다.
- 진단: 필요한 tool 호출은 실제 2회인데 fixture가 한도 3에서 실패해야 한다고 가정했다. 단일 진단 실행의 안전한 status/counter 출력은 `.agent/input/P0-026-budget-diagnostic.log`. 현재 정상 처리는 completed, tool_calls=2였으며 예산 초과가 아니었다.
- 두 번째 실패: V1 11 passed, V2 54 passed/3 failed. Benchmark template의 최소 tool budget은 3이므로 한도 1은 TeamSelector에서 생성 전에 거절되어 team 조회가 404였다. 제품의 정상적인 사전 거절을 실행 중 초과와 혼동한 fixture 문제다. `.agent/evidence/P0-026/completion-worker-0927014920/`에 보존했다.
- 마지막 보수 `d6aaa91`: 정상 한도 3 완료, 한도 1의 팀 생성 전 거절, 유효한 한도 3에서 검색을 반복하는 experiment_runner fixture의 실행 중 예산 초과를 각각 검사한다. 실제 Retrieval 호출 앞에서 4번째 시도를 차단하고 실제 호출 3회/partial/no draft를 확인한다. 제품 제한·보안 AC·성능 목표는 변경하지 않았다.
- 검증 진행: 세 번째이자 마지막 worker 명령 실행 중(`.agent/input/P0-026-completion-worker3.log`). 결과가 나오기 전 통과로 기록하지 않는다. scoped ruff check/format은 통과.
- 정확한 다음 행동: worker3의 V1~V4 실제 결과와 scenario/variant reports를 읽고 V5 수동 관찰을 기록한다. 통과하면 submit→main 통합→target 검증→close. 세 번째도 실패하면 추가 반복 대신 실패 증거와 해소 조건을 남긴다.
- 별도 보조 증거: current core `93ae0a3` + pinned harness `ea76550`의 Ultra/4096 모델 대표 9회는 모두 완료/30초 내/금지 marker 0. native 모델 API 외에는 local/mock이며 E2E-06/08 semantic quality는 not_run. 이 결과를 controlled tests에 섞지 않는다.
- 미검증: 실제 팀원 Response/Runtime identity, 전체 브라우저 E2E, 실제 GPU 실험 및 선택 확장. standalone OpenShell·NemoClaw·Chrome smoke는 각 독립 증거를 참조한다.
