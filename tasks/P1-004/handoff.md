# P1-004 — 비서 intent 라우팅과 얇은 실행 경계

## 구현 사실 (task/P1-004 cc7d0a8)

- graphs/supervisor.py route_intent: 순서가 고정된 결정적 규칙(삭제·배포·결제 등 unsupported → 예약 → 피드백 → 저장 → 외부 초안 → 연구·실험 동사 task_run → 기본 query). 같은 입력은 같은 결정. 벤치마크를 언급만 하는 질문은 query다. 도메인이 없으면 domain_not_resolved와 다음 선택지.
- internal/public 채널은 Supervisor를 거쳐 query/external_draft만 가능(그 외 channel-scope unsupported).
- WorkService.assist: store_note는 owner KB write(동일 텍스트 멱등), query/external_draft는 domain 경로(Task/team 없음), task_run은 명시적 팀 경로, follow-up은 저장된 Task goal/pattern 유지. AssistantResponse 공통 결과(status/stop_reason/partial/next_options). POST /v1/assistant.

## 검증

- tests/test_supervisor_boundaries.py(결정성 표 8, 채널 2, 저장/질의 무팀·Task 재사용·unsupported, 예산 partial·HTTP) + test_graph/test_api 회귀 51 passed(개발 확인).

## 제한

- 예약·피드백 intent는 P0-022/P1-005B 구현 전까지 unsupported. LLM 의도 분류 없음(결정적 규칙).

