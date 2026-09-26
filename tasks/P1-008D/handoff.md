# P1-008D — 합성 handler와 영속 lifecycle 로컬 runtime 대체 모듈

## 구현 사실 (wip/P1-008D 216a8c3, P1-008C 위)

- reference/local_runtime_store.py(별도 SQLite journal), local_runtime.py(TeamSpec prepare/cleanup, AgentSpec+TaskRequest run/status/cancel, 등록된 합성 handler만, owner/domain/member/role/tool/budget 확인, OpenShell·미지원 capability 명시 거절(501), 동시 동일 key 1회 실행·다른 payload conflict, 재시작 시 in-flight unknown·자동 replay 0, sandbox_id 없음). local_security 재사용.
- RuntimeHttpAdapter run/status/cancel이 ASGI로 동작(sandbox_id=None). 실행 중 status는 in-process LocalRuntime과 같이 404, /journal은 running.

## 검증

- tests/test_local_runtime.py 19 passed(5회 반복 안정). OS sandbox/OpenShell/다영 runtime이 아니다.

## 다음

- RuntimeHttpAdapter prepare/cleanup 추가와 bootstrap 지원 표시는 P1-008(공유 파일) 범위.

