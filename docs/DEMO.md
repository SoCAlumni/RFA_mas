# 합성 데모와 E2E 인수 하네스 (P0-026)

이 문서는 합성 end-to-end 데모 명령(`rfa demo --full`)과, `RFA_E2E_Test_Scenarios_10_ko.md`를 controlled 모드로 재생하는 하네스의 실행법과 판정 규칙을 적는다. 여기서의 통과는 실제 API·서비스·SQLite·LangGraph와 실제 `rfa api` 프로세스를 결정적인 mock/local adapter와 함께 실행한 API 통합 결과다. 실제 NVIDIA 모델, NeMo Retriever, 팀원 서비스, OpenShell, UI 검증이나 전체 E2E 통과를 뜻하지 않는다.

## 전체 데모: `rfa demo --full`

```sh
.venv/bin/python -m rfa_mas demo --full --data-dir /tmp/rfa-demo-run
```

- `--data-dir`는 비어 있거나 없는 경로여야 한다(비어 있지 않으면 `demo_data_dir_not_empty`로 거부하고 아무것도 쓰지 않는다). 생략하면 새 임시 경로를 만들고 보고서의 `data_dir`에 적는다. 끝난 뒤 그 경로를 지워도 된다.
- 키·GPU·Docker·네트워크 없이 1분 안에 끝난다(측정: 약 3~5초). `.env`와 `--env-file`은 읽지 않는다(`--env-file`을 주면 `demo_configuration_rejected`). 고정 구성은 mock 모델·mock 검토·mock 게시, local 검색·정책·runtime·trace, 수동 시계 scheduler다.
- 입력은 `fixtures/demo/materials.jsonl`의 합성 자료 6개다: 실험 메모(benchmark A 로그), issue export(#17, benchmark B 결과와 checksum 확인 할 일), 내부 잠정 계획, 공개 FAQ, 개인 1:1, 개인 GPU 자원 메모. 각 줄의 `markers`는 비공개 canary와 내부 전용 사실이며 출력 검사에 쓴다.
- 호출 주체는 이 설치의 로컬 소유자(키 없는 loopback, 로컬 개발용)이고, 앱은 같은 프로세스 안에서 ASGI transport로 호출한다. 조직 membership 등록 경로가 아직 없으므로 내부 자료는 `owner`, 개인 자료는 `private`, FAQ는 `public` 대상이다.

단계와 `mode`:

| 단계 | 내용 | mode |
| --- | --- | --- |
| 1 ingest | 자료 6개 저장(note API, issue는 import API), source/revision 영수증 | local |
| 2 derive_and_candidates | 결정·요약·할 일 도출과 Todo 후보(마감·blocker) | local (규칙, 모델 없음) |
| 3 benchmark_team_task | Benchmark 팀 실행, A 10.0ms/81.0% 대 B 8.2ms/80.8% → 지연 -18.0%, 정확도 -0.2%p | simulated (합성 로그 파싱, GPU·모델 실측 아님) |
| 4 follow_up_reuses_team | 같은 세션·Task의 후속 실행이 같은 team을 재사용 | simulated |
| 5 owner_internal_status_answer | 소유자 대상 상태 답변, 내부 자료 사용, 개인 자료 미사용 | mock |
| 6 external_public_draft | 외부 요청(ingress public) → 공개 FAQ(또는 FAQ만에서 도출된 항목)만 근거인 public DRAFT | mock |
| 7 edit_invalidates_approval | 본문 수정 → 승인 무효(`draft_changed`), 게시 시도 409 `approval_required` | mock |
| 8 re_review | 재검토가 수정된 버전에 승인을 다시 묶음 | mock |
| 9 publication | mock 게시 1회, 같은 키 재요청은 같은 영수증, 다른 키는 409 `publication_exists` | mock |
| 10 scheduled_briefing | 브리핑 일정 + 수동 시계 runner, 같은 발화를 두 번 tick해도 실행 1회 | local (수동 시계) |
| 11 restart_same_database | 같은 DB로 새 container: 세션·실행·팀·DRAFT·게시·일정 실행 보존, 재요청해도 중복 게시 없음 | local |

보고서(stdout JSON)는 ID·상태·stop reason·source/policy/approval/receipt 참조·단계별 `mode`·어댑터 목록·`checks`·`not_run`만 담고 자료 본문은 담지 않는다. `privacy`는 ingest 확인 응답(소유자 자신의 입력 echo)을 뺀 모든 API 응답, trace 파일, 보고서 자체에서 비공개 canary를 세고, 공개 출력에서는 내부 전용 사실까지 센다. 모든 `checks`가 참이면 종료 코드 0, 아니면 1이다. 실제 모델(P1-002A), 실제 게시·팀원 서비스(P1-008A), OpenShell runtime(P1-007B/C), NAT(P0-028), UI(P0-025A)는 `not_run`에 적고 실행했다고 주장하지 않는다.

검증: `.venv/bin/python -m pytest -q tests/test_demo_e2e.py`는 이 명령을 실제 하위 프로세스로 임시 경로에서 실행하고(최소 환경, IP 소켓·DNS 차단 확인) 보고서를 검사한다.

인자 없는 기존 `rfa demo`(단일 query→draft)는 바뀌지 않았다.

## E2E 하네스 실행

```sh
RFA_E2E_REPORT_DIR=/tmp/rfa-e2e-reports .venv/bin/python -m pytest -q tests/e2e
```

- 보고서는 `$RFA_E2E_REPORT_DIR/<UTC 시각>-<id>/`에 시도별 JSON과 `summary.json`으로 쌓이고, 마지막 줄에 경로가 출력된다. 경로를 주지 않으면 pytest 임시 경로를 쓴다.
- 키·GPU·Docker 없이 동작한다. IP 소켓과 DNS는 차단되며, 실제 앱 프로세스를 쓰는 E2E-10 테스트만 127.0.0.1 loopback을 연다.
- `tests/e2e/test_real_model.py`의 실제 모델 gate는 opt-in이다. `RFA_ENV_FILE=<명시한 env 파일의 절대 경로>`가 없거나 그 파일에 `NVIDIA_MODEL`/`NVIDIA_API_KEY`가 없으면 항목 3개를 not_run(P1-002A)으로 기록하고 이유와 함께 skip한다. 이 경우 전체 명령 결과에 skip 1건이 나온다. 같은 파일의 guard 자체 검사 3건은 항상 실행된다. skip을 전체 real gate 통과로 집계하지 않는다.

## 실제 모델 gate (opt-in)

```sh
RFA_ENV_FILE=/absolute/path/to/.env.dev RFA_E2E_REPORT_DIR=/tmp/rfa-e2e-real \
  .venv/bin/python -m pytest -q tests/e2e/test_real_model.py -rs
```

- P1-002 제품 경로(`build_container(..., model_transport=...)` → ModelPort → NVIDIA adapter, public-only egress gate)를 쓴다. env 파일에서는 `NVIDIA_BASE_URL`/`NVIDIA_MODEL`/`NVIDIA_API_KEY`만 읽고 `model_provider=nvidia`를 고른다. 나머지는 controlled 구성(임시 DB, local 검색·정책·runtime, mock 검토·게시)이다. 설정 값은 출력하거나 저장하지 않는다.
- 항목: E2E-02 공개 질문(G03, owner), E2E-06 공개 persona(external의 첫 질문), E2E-08 공개 초안(/v1/assistant, ingress public, 게시하지 않음). 모두 public 대상이며 항목마다 `real_model_repeats`(3)회 순차 호출한다.
- 저장소에는 비공개 합성 노트(R07/R08, 시드 owner canary)도 넣어 "외부로 나간 요청의 비공개 canary 0건"을 실제로 검사한다. 모델 transport는 비공개 canary나 내부 전용 사실이 든 본문, 설정된 endpoint 외의 host를 보내기 전에 거부하고, socket guard는 그 host 하나의 DNS·연결만 허용한다.
- 보고서: 호출별 모드(real/mock)와 소요 시간(제품 관측 ledger), 보낸 model id, endpoint host, API 지연(n), 핵심 사실 포함률, real_integration gate의 실제 판정과 근거 수치.

## 구성

- `fixtures/e2e/`: R01~R10·변형·M01·persona 4종·열람 매트릭스·golden 20문항·1,000문서 생성 seed·실행 전 고정한 목표치.
- `tests/e2e/harness.py`: 격리 container, persona별 인증 경계, API ingest, loopback 전용 네트워크 guard, `rfa api` 자식 프로세스(`LocalServer`), 보고서 기록기.
- `tests/e2e/test_rfa_scenarios.py`: E2E-01~09 controlled 재생(핵심 사례 3회 반복), E2E-05 OpenShell 증거 참조, 미구현 변형의 not_run 기록.
- `tests/e2e/test_process_recovery.py`: 실제 앱 프로세스 SIGKILL→재기동→재개/취소, 스케줄러 재시작 coalescing. kill/restart 시도 내내 두 번째 SQLite 프로세스(`SecondSqliteReader`, 별도 scheduler 프로세스와 같은 접근)가 같은 DB를 짧게 열고 닫으며, 그 옆에서 동시 요청 20건을 보낸다. 서버 로그의 `disk I/O error`/SIGBUS 0건을 검사한다(P0-005A로 고친 결함. 수정 전 코드에서는 3/3 실패).
- `tests/e2e/test_load.py`: 실제 앱 프로세스 대상 n=100 정상 부하와 전체 활성 worker 상한.
- `tests/e2e/test_real_model.py`: opt-in 실제 모델 gate와 그 guard의 자체 검사.

## 보고서 판정

gate는 서로 섞지 않는다.

- functional_security: 기록한 check가 모두 통과하면 passed, 하나라도 실패하면 failed(실패 check ID만 기록), check가 없으면 not_run.
- quality: 목표가 고정된 지표만 passed/failed로 판정한다. 실패 지표는 소유 task를 함께 남긴다.
- performance: 고정 목표가 있는 측정만 판정한다(소표본은 max, n=100은 p95). controlled 측정이며 실제 모델 지연이 아니다.
- real_integration: controlled 모드에서는 항상 not_run이며 소유 task를 적는다. E2E-08 HTTP 변형은 P1-008C local stand-in이고 실제 서비스가 아니다. opt-in 실제 모델 gate만 실행 결과로 passed/failed를 기록하며, functional_security가 통과하지 않으면 passed가 될 수 없다. 이 판정은 모델 경로에 한정되고 검토·게시·runtime은 여전히 local/mock이다.

`status`는 functional_security만 따른다. `complete_e2e`는 모든 gate가 passed이고 미실행 항목이 없을 때만 true이므로 controlled 모드에서는 항상 false다.

## identity

제품은 설치 소유자 한 명만 인증하고 조직 membership을 등록하는 경로가 아직 없다. 하네스는 임시 DB의 설치 소유자 레코드를 fixture `owner` persona로 바인딩해 소유자 호출이 실제 인증 dependency를 거치게 한다. 나머지 3개 persona는 인증 dependency가 고정 fixture principal을 돌려주는 별도 앱 인스턴스를 쓴다. 요청 본문이나 문구로 identity를 바꾸지 않는다.

## E2E-05

controlled 모드는 적용하지 않는다. 실제 standalone OpenShell 증거는 통합된 [OpenShell 기록](evidence/openshell.md)(P1-007C, run `0926181417`)이며 이 하네스는 다시 실행하지 않는다. Supervisor 메시지 경계만 controlled로 검사한다. standalone 역할 정책과 실제 RFA RuntimePort identity의 연결은 다른 gate다. [NemoClaw 운영 기록](evidence/nemoclaw.md)도 별도이며 host RFA backend는 sandbox 밖이다.
