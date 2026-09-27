# 팀원 교체 전 로컬 PoC

```sh
uv sync --locked
uv run python -m rfa_mas.poc --data-dir .local/poc --port 8780
```

브라우저에서 <http://127.0.0.1:8780/ui/>를 연다. `Ctrl-C`로 종료하며 같은 명령으로
다시 실행하면 세션·노트·승인 대기·모의 게시 영수증이 유지된다. 포트가 사용 중이면
`--port 8785`처럼 변경한다. 같은 data-dir의 서버 두 개는 거절한다. 데이터를 지우지 않는다.

이 실행은 **합성 데모용 single-user local/mock**이다. `.env`를 읽지 않으며 ambient
provider 환경변수도 적용하지 않는다. 키/GPU/Docker/팀원 서버가 필요 없다. NVIDIA
실호출은 기존 opt-in 실행 경로를 사용하며 이 명령에서 몰래 활성화하지 않는다.

## 화면에서 한 번 완주

1. 새 세션을 만든다.
2. 노트에 합성 메모를 저장한다(기본 비공개). owner 질의로 내용을 확인할 수 있다.
3. 공개 FAQ 예시: 도메인 `triv3`, 대상 `public`, 질의 `TRIV3 공개 트랙`을 실행한다.
   기본 설치의 공개 합성 fixture가 근거다. 개인 노트가 공개 FAQ로 자동 승격되는 것이 아니다.
4. `waiting_approval`을 확인하고 검토 목록을 새로고침한다. 본문과 대상을 읽은 뒤
   `승인` 또는 `거절`/`수정 요청`을 누른다. 자동 승인은 없다.
5. 실행 결과의 검토 새로고침 버튼으로 재개한다. 승인된 카드의 `모의 게시`를 누르면
   `mock` 영수증이 표시된다. 실제 인터넷 게시나 MCP WRITE는 하지 않는다.
6. 서버를 종료·재기동한 뒤 세션과 승인 기록을 확인한다. 승인 대기 상태에서 재시작해도 된다.

## 조립·교체 경계

| 부분 | 이 PoC | 팀원 모듈 교체 위치 |
| --- | --- | --- |
| core/KB/session | 기존 FastAPI·LangGraph·SQLite/checkpointer | 그대로 유지 |
| runtime | 기존 core `LocalRuntime`, OS sandbox 아님 | 기존 RuntimePort / runtime adapter·capability 연결 |
| 검토/게시 | 별도 SQLite의 수동 검토 원본·mock receipt | ResponsePort / Publisher·HTTP mapper |
| UI | 기존 동일 출처 UI | UI 전체 교체 또는 `UpstreamTarget` HTTP transport 교체 |
| 조립 | `src/rfa_mas/poc/bootstrap.py` | graph를 바꾸지 않고 조립/mapper 교체 |

TCP에 노출되는 것은 UI 한 포트뿐이다. core/review는 같은 프로세스 안에서 HTTP 계약을
ASGI transport로 호출한다. `18781/18782`는 내부 논리 URL이며 실제 listener가 아니다.
서비스 token은 시작 때 메모리에서 생성하고 browser·로그·DB에 저장하지 않는다.

`PocResponse`는 core의 1.0 DRAFT를 검토 서비스의 1.1 계약으로 변환한다. 실제 로컬
`ResumePolicy` 검사 뒤 policy 참조를 붙이며 승인에는 version/hash/target/source/policy를
결합한다. `UiReviewTransport`는 모의 게시도 core publication API를 통과시킨다.
따라서 승인 뒤 정책/자료가 바뀌면 거절되고 core의 durable idempotency 기록이 유지된다.
이는 로컬 application policy이며 OpenShell 강제를 의미하지 않는다.

저장소는 `data-dir/core/`, `data-dir/review/`, `data-dir/traces/`로 분리한다. UI는
별도 DB가 없다. `poc.lock` 파일은 남아도 프로세스가 끝나면 OS lock은 해제된다.
파일을 지워 lock을 우회하지 않는다. 로컬 OS 계정은 신뢰 경계이며 LAN 공개용이 아니다.

기본 UI는 세션·노트·질의·수동 검토·모의 게시 범위다. 예약/Task 팀 관리 화면은 없다.
팀·예약의 기존 core API와 `uv run rfa demo --full`은 별도 경로로 유지된다.
NVIDIA/OpenShell/NemoClaw 실제 증거 및 팀원 교체 gate는 이 PoC 통과로 덮어쓰지 않는다.

## 검증

```sh
uv run python -m pytest -q tests/test_poc.py
```

실제 loopback subprocess 시작·세 번 재시작, 승인 전 거절, 현재 정책 재검사, 동일 게시
멱등성/다른 key 거절, 영속 receipt, Host/Origin/CSRF, 포트 충돌, 중복 데이터 경로,
ambient provider 격리를 검증한다. 제품 전체 real/UI E2E의 통과를 뜻하지 않는다.
