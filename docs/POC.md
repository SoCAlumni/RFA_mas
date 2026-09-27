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

1. **비서 채팅**에서 `아틀라스 프로젝트의 마감은 10월 2일입니다.`를 보낸다.
   정보형 문장/`메모: …`/`… 기억해줘`를 서버가 저장으로 분류하고 비공개 KB에 저장한다.
2. `아틀라스 프로젝트 마감 알려줘`를 보내면 저장한 자료를 검색해 근거를 보여준다.
   Enter는 전송, Shift+Enter는 줄바꿈이다. 새 대화와 최근 대화를 선택할 수 있다.
3. **내 KB** 탭에서 제목·내용 필터, 자료 공간 선택, 본문·공개 범위·출처·버전을 확인한다.
4. `TRIV3 공개 초안 만들어줘`를 보내고 **승인함**에서 본문·대상을 확인한 뒤 수동으로
   승인/거절/수정 요청한다. 결정 후 기존 core 재개 API로 상태를 조회한다.
   승인된 카드의 `모의 게시`는 mock 영수증만 만든다. 개인 메모는 공개 근거가 아니다.
5. 새로고침/재시작 후 대화를 이어간다. 저장 메시지·응답 참조·승인 대기가 보존된다.

라우팅은 명시적으로 **로컬 규칙 기반**이며, 담당 경로의 답변은 기존 **mock 모델**이다. 모호한 입력은
확인 안내를 반환한다. 임의 대명사 해석/장기 대화 추론, 자동 일정·삭제·도구 실행은
지원하지 않는다. 담당 선택은 기본 **자동**이다. 현재 사용자의 active/ready Task 목표에
관련 주제가 있으면 기존 Task/팀을 재사용한다. 동점 후보는 억지로 고르지 않는다.
Task가 없어도 TRIV3/양자화의 명시적 주제는 해당 도메인 담당이 처리하며, 이는 영속 Task 팀이 아니다.
그 밖의 질문은 **비서 직접 처리**로 두 자료 공간의 현재 허용 KB를 검색하고 원문 발췌를 보여준다.
근거가 없으면 부족함을 알리며 새 팀을 만들지 않는다. 담당 미지정 메모의 저장 공간만 기존
기본 KB(TRIV3)를 재사용하며 이는 TRIV3 worker 배정/실행이 아니다. 관련 메모는 담당 Task의
자료 공간에 저장하고 task/team 참조를 대화에 남긴다(저장만으로 worker를 실행하지 않는다).
담당을 직접 지정할 수도 있다. 제목/목표 주제 기반 규칙은 의미 모델이 아니므로 임의 동의어·대명사
해석을 보장하지 않는다. no-match 공개 요청은 public 근거 미리보기이며 승인용 Draft를 만들지 않는다.
현재 질문에 키워드를 포함해 주세요. 저장/검색을 사용자가 별도 폼에서 선택할 필요는 없다.

**담당 단위는 Task 팀이다.** Task 하나에 TeamInstance 하나가 대응하고, 같은 Task의 후속 Run은 같은
팀을 재사용한다. 드롭다운 "담당 선택"은 `GET /ui/api/chat/assignees`가 돌려주는 현재 사용자의
Task 팀(목표·패턴·상태)을 우선 표시하고, 자료 공간(TRIV3/양자화)은 저장 위치를 고를 때 쓰는 보조
선택지다. Task 팀을 명시 선택하면(`task_id`) 서버가 소유권·active/ready를 다시 확인한 뒤 그 팀으로
실행하며, 다른 사용자/없는/사용 불가 Task는 대화에 남기지 않고 404/409로 거절한다.

새 Task 팀은 사용자가 **명시적으로** "…조사해/검증해/분석해/비교해/벤치마크 돌려"라고 요청했을 때만
만든다(`task_run`). 적합한 기존 Task 팀이 있으면 그 팀을 재사용하고, 없으면 core Supervisor의
team 경로(`TeamExecutionRequest`; 패턴은 벤치마크 용어 유무로 Benchmark/Research)로 정확히 하나의
Task+팀을 만든다. 같은 메시지 재전송은 멱등 재조회이며 두 번째 팀을 만들지 않는다. 이후 관련
질의는 자동으로 그 Task 팀에 배정된다. 메모/일반 질의만으로는 Task를 만들지 않는다. 실험 수치는
mock(simulated)이며 실측이 아니다.

**처리 상태 카드.** 대화 버블 위에 고정(sticky)된 카드가 마지막 요청의 처리 상태를 답변 본문과 별도로
목록으로 보여준다. 스트리밍 중에는 "진행 중"으로 단계가 도착하는 대로 추가되고, 완료 후에도 유지되며
새로고침하면 마지막 턴의 상태가 복원된다(단계 detail은 대화 DB의 `chat_stages`에 보존). 단계는
`understanding`(의도·규칙) → `routing`(담당 종류·사유·검토한 Task 팀 수·후보와 공통 주제) →
`team`(기존 팀 재사용: 패턴·상태·runtime·역할별 agent_id/capabilities) 또는 `team_spawn`(새 팀: 패턴·예정
역할) → `preparing` → `team`(새로 생성된 실제 구성) → `team_result`(실행 상태·역할별
succeeded/steps/tool calls·simulated) → `completed`(결과·run/source ID)이다. detail에는 메모 원문·답변·
키가 들어가지 않는다. 역할 구성은 core TeamFactory가 결정한 실제 값이며 예정 역할은 승인된 템플릿 정보다.

## 조립·교체 경계

| 부분 | 이 PoC | 팀원 모듈 교체 위치 |
| --- | --- | --- |
| core/KB/session | 기존 FastAPI·LangGraph·SQLite/checkpointer | 그대로 유지 |
| runtime | 기존 core `LocalRuntime`, OS sandbox 아님 | 기존 RuntimePort / runtime adapter·capability 연결 |
| 검토/게시 | 별도 SQLite의 수동 검토 원본·mock receipt | ResponsePort / Publisher·HTTP mapper |
| UI | 채팅/KB/승인함, 동일 출처 plain DOM | UI 전체 교체 또는 `UpstreamTarget` HTTP transport 교체 |
| 채팅 | `ChatPort` + `poc/chat.py`의 로컬 turn ledger | ChatPort 구현/고정 core HTTP consumer 교체 |
| 담당 목록 | `TeamCatalogPort` + `poc/catalog.py` read-only owner ID 조회 및 기존 repository 재검증 | 이후 core task-list HTTP adapter로 교체; 다른 서비스 DB 공유 아님 |
| 조립 | `src/rfa_mas/poc/bootstrap.py` | graph를 바꾸지 않고 조립/mapper 교체 |

TCP에 노출되는 것은 UI 한 포트뿐이다. core/review는 같은 프로세스 안에서 HTTP 계약을
ASGI transport로 호출한다. `18781/18782`는 내부 논리 URL이며 실제 listener가 아니다.
서비스 token은 시작 때 메모리에서 생성하고 browser·로그·DB에 저장하지 않는다.

`PocResponse`는 core의 1.0 DRAFT를 검토 서비스의 1.1 계약으로 변환한다. 실제 로컬
`ResumePolicy` 검사 뒤 policy 참조를 붙이며 승인에는 version/hash/target/source/policy를
결합한다. `UiReviewTransport`는 모의 게시도 core publication API를 통과시킨다.
따라서 승인 뒤 정책/자료가 바뀌면 거절되고 core의 durable idempotency 기록이 유지된다.
이는 로컬 application policy이며 OpenShell 강제를 의미하지 않는다.

저장소는 `data-dir/core/`, `data-dir/review/`, `data-dir/traces/`, `data-dir/chat/`으로
분리한다. 채팅 저장소는 사용자 메시지와 source/run 참조만 보관한다. 검색 답변은 매번
core의 현재 ACL 검사를 거쳐 재구성하며 원문 응답 캐시를 재사용하지 않는다. 비서 직접 검색도
원문 대신 revision/ACL 참조를 저장하고 재조회 때 core repository의 현재 정책을 다시 확인한다.
처리 단계와 담당 참조는 대화 DB에 보존한다. 브라우저에는
대화를 영속 저장하지 않는다. 이는 암호화 저장이 아니므로 신뢰된 로컬 계정에서 사용한다.
`poc.lock` 파일은 남아도 프로세스가 끝나면 OS lock은 해제된다.
파일을 지워 lock을 우회하지 않는다. 로컬 OS 계정은 신뢰 경계이며 LAN 공개용이 아니다.

대화 session과 LangGraph 실행 thread는 다르다. 담당 에이전트에 배정한 질문마다 독립 실행 session을 만들고
대화 ledger에 run을 연결한다. 승인 대기 초안 때문에 다음 질문이 막히지 않으며, 이를
해결하려고 자동 승인하거나 기존 checkpoint를 덮어쓰지 않는다. 개인 답변도 core의
검토 대상 초안이며 실행 정보에서 실제 상태를 표시한다. 공개 초안만 승인함에 노출한다.
개인 답변 표시가 게시 승인이나 제품 run 완료를 의미하지 않는다. 비서 직접 검색은 모델/worker
실행이 아니므로 존재하지 않는 Run/Task/승인 ID를 만들어 채우지 않는다.

`GET /ui/api/chat/sessions`, `GET/POST /ui/api/sessions/{id}/chat`,
`GET /ui/api/notes/{source_id}`는 ChatPort를 주입한 PoC에서 제공한다. 메시지는
`text`, `message_id`, 선택 `domain_id`만 받고 identity/권한/공유 대상 입력은 거절한다.
동일 session/message ID 재전송은 기존 결과 조회이며 다른 내용이면409다. 처리 중 중단된
메시지는 outcome_unknown으로 남기고 재시작이 자동 재실행 권한을 주지 않는다.

`POST /ui/api/sessions/{id}/chat/stream`은 동일 CSRF 경계의 NDJSON 스트림이다.
실제 서버 단계 `understanding → routing(담당자) → preparing → completed`와 최종 `result`를 보낸다.
화면은 chunk를 도착 즉시 읽으며 타이머로 단계를 연출하지 않는다. 입력 저장은 저장 준비/완료로 표시한다.
LLM 토큰/내부 사고/세부 worker 노드를 스트리밍하는 것은 아니다. 매우 빠른 로컬 실행은 단계가
거의 동시에 보일 수 있고 완료 후에도 타임라인은 남는다. 연결 중단은 대화 재조회로 확인하며
자동 재전송하지 않는다. 오류는 안전한 메시지만 전송한다. 중복 message ID에는 저장된 결과만 반환한다.

기본 UI는 채팅 저장·검색·공개 초안·수동 검토·모의 게시 범위다. 예약/Task 팀 관리 화면은 없다.
팀·예약의 기존 core API와 `uv run rfa demo --full`은 별도 경로로 유지된다.
NVIDIA/OpenShell/NemoClaw 실제 증거 및 팀원 교체 gate는 이 PoC 통과로 덮어쓰지 않는다.

## 검증

```sh
uv run python -m pytest -q tests/test_chat_routing.py tests/test_chat_poc.py tests/test_local_ui.py tests/test_poc.py
```

실제 loopback subprocess 시작·세 번 재시작, 승인 전 거절, 현재 정책 재검사, 동일 게시
멱등성/다른 key 거절, 영속 receipt, Host/Origin/CSRF, 포트 충돌, 중복 데이터 경로,
ambient provider 격리, 채팅 자동 분류·현재 권한·동시 메시지 중복·대화 복구를 검증한다.
제품 전체 real/UI E2E의 통과를 뜻하지 않는다.
