# OPS-004 — taskctl 호출 단위 git 조회 memo

- 목표/AC: AC1 같은 호출 안의 동일 git 조회 1회·결과 재사용, AC2 memo 밖/호출 사이 변경은 새로 조회, AC3 기존 회귀 유지.
- 원인(측정 사실): cProfile에서 `taskctl ready` 22.2초 중 `complete()→valid_evidence()→source_manifest()`가 약 20초, git subprocess 1,551회.
- 구현: `scripts/tasklib/store.py`의 `git_memo()` context manager와 `git()` 캐시(성공 stdout 또는 거절 메시지). `cli.main()`만 memo를 연다. `execute()` 직접 호출(테스트 포함)은 기존대로 매번 조회한다.
- 불변: fingerprint/파일 hash 비교, fencing, scope, 계약 수락 규칙은 변경 없음. taskctl은 git을 변경하지 않으므로 한 호출 안의 판정은 하나의 snapshot이다.
- 검증: `tests/test_taskctl.py` 100 passed(새 memo 테스트 3개 포함). 같은 control 상태에서 `ready`의 CPU 시간이 약 11.5초에서 1.7초로 줄었다. 실제 wall time에는 lock 대기가 포함될 수 있다.
- 한계: 파일 hash 재계산과 YAML 파싱 비용은 그대로다. 제품 기능 검증이 아니다.
- 다음 행동: 없음(통합 후 OPS-000/001/003 control 재검증은 coordinator cascade에서 처리).

