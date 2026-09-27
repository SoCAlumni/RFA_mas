# P0-005A — SQLite 다중 프로세스 안전성

## 원인 (자식 프로세스 lock 관찰로 증명)

- POSIX에서 파일 descriptor를 닫으면 그 프로세스가 해당 파일에 건 lock이 모두 풀린다.
- `_private_files()`는 연결할 때마다 DB·-wal·-shm을 열고 닫았다. 연결이 살아 있는 동안에도 그랬다.
- 그 결과 -shm의 SQLite dead-man lock이 풀렸다. 다른 프로세스가 shm을 재초기화했고, 그것을 mmap하고 있던 서버가 SIGBUS로 죽었다.

## 수정 (wip/P0-005A 85eea7d, ledger_worker)

- 기존 파일 검사는 lstat와 경로 기반 chmod만 한다. DB가 없을 때만 setup lock 안에서 배타적으로 새로 만든다.
- sidecar는 삭제 경쟁 중일 수 있으므로 링크 수 0 또는 1을 허용한다. DB는 정확히 1이어야 한다.
- `reference/local_security.py`의 같은 패턴도 고쳤다.
- `_connect`는 DB가 없으면 더 이상 몰래 만들지 않는다(configuration_error). 생성은 `initialize()`만 한다.

## 측정 (진단 스크립트, 요청 40회)

| 코드 | 두 번째 reader 없음 | 두 번째 reader 있음 |
|---|---|---|
| 수정 전 | - | 2/2 SIGBUS |
| 수정 후 | 3/3 40/40 | 6/6 40/40 |

- 개발 중 회귀: 1/40 요청이 503(sidecar 링크 0 경쟁)이었다. cycle 1에서 수정했고, 이후 in-process 240/240이다.

## 검증 (개발)

- tests/test_sqlite_multiprocess.py 6개. 실제 child lock 관찰, 실제 `rfa api` 프로세스와 reader 동시 실행 포함.
- 수정 전 코드에서는 원래 3개가 실패하고 API가 -10으로 종료된다.
- 인접 테스트 221 passed. 전체 1055 passed.

## 재검증 final-e2e-after-trace (2026-09-27T01:19Z)

- 사유: Resume remaining dependency checks after trace fixture correction; do not repeat completed tasks; product source fixed
- 소스 변경 없이 현재 통합 HEAD 93ae0a3에서 계획된 검증을 worker/target 단계로 재실행한다.
