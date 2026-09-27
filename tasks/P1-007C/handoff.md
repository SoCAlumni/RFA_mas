# P1-007C — 로컬 OpenShell 실제 역할별 허용·차단(E2E-05)

- 구현(wip/stack ae21a4b, 0d3cb82, 2074321, 260f394; nemoclaw_research 개발·실행)
  - deploy/openshell 정책·rootfs 빌드, scripts/openshell_e2e05.py orchestrator, opt-in live 테스트, docs/evidence/openshell.md.
  - EDUCATION_MAPPING의 OpenShell 행이 이 증거를 가리킨다.
- 실제 실행(real OpenShell v0.1.1 VM driver, local standalone, run 0926181417)
  - 14행 매트릭스가 모두 기대와 일치했다. 역할별 sandbox n=3.
  - R1 허용 읽기, R2 L7 경로 거절, R3 포트 거절, R4 비승인 binary 거절, R5 sentinel 거절(control sandbox 대조), R6 inconclusive, R7 advisor 제안 후에도 거절.
  - E1 승인 계산, E2 /tmp 쓰기 거절, E3 비승인 실행 거절, E4 네트워크 거절, E5 inconclusive.
  - 한계로 기록한 항목: R4b 자식 bash 허용, E6 workdir 복사 binary 실행 가능.
  - SupervisorBus: worker→worker는 direct_message_denied, worker→Supervisor는 전달(앱 경계, 별도 판정).
  - 지연 median: cold 준비 약 7.1s, warm 허용 읽기 9.5ms, L7 거절 9.2ms.
- 검증: 기본 테스트는 OpenShell 없이 결정적 분류 규칙과 SupervisorBus 4개를 확인한다. 실제 결과는 report.json 검토(V2)로 확인한다.
- 한계: teammate runtime/identity(P1-008B), 제품 RuntimePort가 OpenShell에서 역할을 실행하는 경로, NemoClaw는 not_run이다. 파일시스템 거절은 per-event 로그가 없어 control sandbox 대조로 귀속했다.

