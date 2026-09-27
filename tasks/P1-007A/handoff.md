# P1-007A 기록된 로컬 시연 통합

NemoClaw v0.0.124/OpenShell 0.0.116/OpenClaw 2026.7.1, NVIDIA Super120B를 쓴
기존 실제 n=1 실행의 preset/probes/replay 테스트/안전한 JSON을 통합한다.
RFA host backend는 sandbox 밖이며 mock 모델을 썼다. 인증은 설치 owner,
역할별 RuntimePort identity·teammate UI gate는 이 결과에 포함하지 않는다.

허용 4 route의 5 요청은 200/201, 나머지 7 probe는 network/L7 정책 거절과 OCSF가 일치한다.
Agent healthz 도구 호출도 있었으나 CLI replayInvalid=true 종료 1과 모델의 거절 오표현을
숨기지 않는다. 실제 정책 판정은 실행/OCSF 기록을 사용한다. credential binding은 미검증.

교육 표 충돌은 기존 standalone OpenShell 행을 보존하고 최신 NemoClaw 실제 행을 반영했다.
P1-008B 의존성은 이 REST 시연에 필요하지 않음이 확인되어 분리했다. 원래 역할별 identity
요구와 gate는 P1-007B/P1-008B에 그대로 남는다.
다음 행동: 최종 문서에서 이 기록의 시각·n=1과 현재 검토 시각을 구분한다.
