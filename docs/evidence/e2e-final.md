# RFA 최종 인수 기록 — 2026-09-27 KST

## 결론과 고정 기준

main `d6aaa91`에서 **controlled/local 인수 75 passed, 실패·skip 0**. 별도 worker에서도 동일 75개가 통과했다. P0-026은 통합 검증 후 done이다. 전체 실제 제품·UI E2E가 완료됐다는 뜻은 아니다.

- V1 demo 11, V2 시나리오/fixture 57, V3 실제 프로세스 복구 6, V4 n=100 부하 1. 핵심 controlled 변형은 3회, 검색 golden20·1000문서 fixture와 seed/clock을 고정했다.
- worker: `.agent/evidence/P0-026/completion-worker-0927015257/`; main: `.agent/evidence/P0-026/completion-integration-0927015649/`. 각 source/result JSON은 명령·시간·spec/계약/source fingerprint를 연결한다.
- 안전한 측정·variant·gate·버전 전체 65개 보고서 projection: [e2e-completion.json](e2e-completion.json). raw artifact의 경로와 SHA-256도 포함한다. 원문·키·Authorization은 복사하지 않았다.
- 환경: Darwin 25.5.0 arm64, CPU 10, Python 3.12.13. 임시 DB/trace, controlled 모델은 deterministic mock. 실측 GPU 실험 없음.
- 기준은 `fixtures/e2e/acceptance_config.json`의 사전 값 그대로다. 실제 모델 30초 등 실패한 기준을 사후 완화하지 않았다. mock token은 null/미수집이며 문자 수와 구별한다.

## 시나리오별 판정

아래 통과는 표에 명시한 경로에 한정한다. JSON에는 variant별 기능/보안·품질·성능·real gate가 각각 있다. `status=passed`만 보고 `complete_e2e=true`로 바꾸지 않는다.

| 시나리오 | 실제 검사한 controlled 경로 | 기능/보안 | 품질·성능과 남은 경계 |
| --- | --- | --- | --- |
| E2E-01 자료 정리 | 20개 입력·부분 실패·중복 upsert·출처/revision | pass ×3 | 결정적 품질/검색 반영 기준 pass; 실제 connector 미실행 |
| E2E-02 근거 질의 | 10.0→8.2ms(-18%)·정확도 -0.2%p, 가설/미확인 구분, golden20와1000검색 | pass ×3 +1000문서1회 | 고정 품질/성능 pass; Ultra 실제 대표 질의×3 별도 |
| E2E-03 팀 | 패턴·재사용·중복·필수 worker 실패·capability·생성 전/실행 중 예산 | pass ×3/변형 | mock 실험을 실측으로 보고하지 않음; 자동 Task 생성 not_run, 의미 품질 not_run |
| E2E-04 선제 제안·예약 | 후보·수동 clock·중복/누락 coalescing·재시작 | pass ×3 | 고정 순위/성능 pass; Debate not_run, wall-clock 항상 실행 보장 없음 |
| E2E-05 격리 | Supervisor 메시지 경계만 controlled | 해당 경계 pass | 실제 standalone OpenShell은 별도 14행 matrix. RFA 역할 identity/runtime 강제는 P1-007B/P1-008B not_run |
| E2E-06 대상별 범위 | 4 fixture identity·canary·권한 사칭·metadata/context 범위 | pass ×3 | 결정적 scope 품질 pass. 실제 사용자 인증 provisioning은 아님; Ultra×3 semantic quality not_run |
| E2E-07 공격 | 공격4종 도달·outbound/write sink·정상 대조·공개 근거로 계속 | pass | 합성 고정 공격만 검증. 실제 LLM 공격 저항 일반화·성능 미측정 |
| E2E-08 승인·게시 | mock/로컬 stand-in, callback·본문/대상/첨부/정책 변경·timeout 조회·중복 | pass ×3/변형 | 실제 게시 없음. Ultra 공개 초안×3 완료, 의미 품질 not_run |
| E2E-09 갱신·기억 | source 수정/회수/삭제·cache 제한·M01 새 세션 적용/철회 | pass ×3/변형 | export/rebuild 선택 변형 not_run; 실제 embedding 갱신 미실행 |
| E2E-10 중단·부하 | 실제 subprocess SIGKILL/재기동, 두 번째 SQLite reader, 취소·자원 반환·일정복구, n100 | pass | pinned local 성능 pass. 중단된 불명확 역할은 재실행하지 않고 조회/취소; 실제 runtime/model 부하 아님 |

기본 `restart-persistence` 변형은 즉시 mock 승인이라 pending human 승인을 다루지 않는다. 이를 숨기지 않고 JSON not_run에 보존한다. 승인 대기 재개는 기존 durable-resume/승인 테스트와 별도 데모·Chrome smoke에서 확인했으며, 실제 승희 승인 원본 검증과 다르다. 전체 UI 10개 시나리오도 미실행이다.

## NVIDIA 실제 비교 — 최대 세 구성으로 종료

제품 core `93ae0a3`, pinned harness `ea76550`으로 같은 공개 합성 E2E-02/06/08을 각3회 호출했다. raw `versions.code_commit`은 harness를 가리키므로 JSON에 `product_commit`을 별도로 명시했다. 이후 `d6aaa91` 통합은 CLI 데모/테스트 추가이며 해당 제품 graph/model 코드는 같다.

| 구성 | 완료/9 | 30초 성능 gate | 관찰 |
| --- | ---: | --- | --- |
| Nemotron 3.5 Lightning,1024 | 7/9 | 세 시나리오 모두 fail | truncated 및 지연; ReadTimeout 1회 재시도 이력 보존 |
| 같은 Lightning,4096 | 8/9 | 세 시나리오 모두 fail | E2E-02 1회 truncated; 최대 124.24초 |
| Nemotron 3 Ultra 550B-A55B,4096 | 9/9 | 세 시나리오 모두 pass | API 지연 최대4.781초; 실제 모델 호출만 real |

Ultra API 지연(ms, 순서대로 각3회): E2E-02 `4780.827,1556.642,1224.563`; E2E-06 `2503.454,1077.844,1332.088`; E2E-08 `2177.711,2605.873,2304.014`. 모든 구성의 금지 outbound marker는 0이었다. E2E-02 지정 사실 검사는 통과했지만 E2E-06/08 semantic quality는 not_run이다. 지연은 소표본이고 보편적 성능/보안 보장이 아니다. 모델 외 검색·정책·runtime은 local, 검토·게시는 mock이다.

재현은 명시 opt-in이며 실제 NVIDIA quota를 사용한다. 기존 `.env.dev`를 수정하지 않는다. 아래 비교 helper는 실제 실행한 실험 로직의 저장소 상대경로판이다. 이 파일 추가 후 live 호출을 다시 수행했다고 주장하지 않는다.

```sh
RFA_ENV_FILE=/absolute/path/to/.env.dev \
RFA_E2E_REPORT_DIR=/tmp/rfa-ultra-comparison \
  .venv/bin/python scripts/e2e_model_comparison.py \
  --max-output-tokens 4096 --model nvidia/nemotron-3-ultra-550b-a55b
```

기본 `tests/e2e/test_real_model.py`는1024로 고정되어 있으므로 위4096 결과를 기본 명령의 결과로 표시하지 않는다. `.env`/기본 provider는 사용자 동의 없이 바꾸지 않았다.

## 기술·UI 보조 증거

- [NVIDIA 모델](nvidia-model.md): 제품 ModelPort live 및 별도 named tool_choice 제안. 제안만 검사한 smoke는 실제 tool 실행이 아니다.
- [NeMo Retriever Skill](nvidia-skill-product.md): 공식26.8.1 CLI→Research worker 실제 경로, n=1. markdown 설치만의 증거가 아니다.
- [NAT](../NAT_COMPATIBILITY.md): installed1.8.0 wrapper+fake provider, 대표 동등성/중복 호출 검사. NVIDIA 실호출이나 streaming/HITL 전체 지원 증거 아님.
- [OpenShell](openshell.md): 이 Mac의 standalone sandbox 역할 정책14행. 제품 RuntimePort identity 연결은 별도 미검증.
- [NemoClaw](nemoclaw.md): 지원 OpenClaw sandbox→제한된 host API n=1. RFA backend는 sandbox 밖이다.
- [LLMOps](llmops.md): Persona24/24 simulated, 실제 Judge 합성 n=1(0.6), 로컬 Langfuse export 및 OSS용 retention job. 품질 점수로 권한 위반을 상쇄하지 않는다.
- [실제 Chrome smoke](ui-smoke.json): Chrome154/Playwright1.63, 기본 세션 생성·노트 저장·XSS-looking 제목 text 렌더링·수동 mock 승인/재개, 외부 outbound/storage/dialog0. 전체10개 UI E2E/실제 UI 서비스 검증이 아니다.

## 실패·재개와 남은 gate

P0-026 worker1/2는 각각11+54 passed/3 failed였다. 동일 budget 변형이3번 실패한 것으로 새 제품 결함3개가 아니다. 첫 fixture는 정상2호출을 한도3 초과로 오인했고, 두 번째는 한도1을 TeamSelector가 생성 전 거절하는 것을 실행 중 초과와 혼동했다. `d6aaa91`은 정상·사전 거절·실제 검색 반복 중 초과를 분리했다. 제품 예산·정책은 완화하지 않았다. 세 번째 worker 및 main은75/75 pass. 실패 evidence와 수정 커밋을 삭제하지 않았다.

P1-007B/P1-008A/B의 실제 역할 identity·팀원 승인/게시 원본은 미검증이다. 사용자 요청으로 마련한 로컬 교체 모듈(P1-008C/D, P0-025A)이 대신 실행되지만 별도 권한을 취득한 것은 아니다. Engineering/GPU실험/고도화 clustering/Debate/추가connector 등 기존 P2는 deferred다.

공식 제출 폼은2026-09-27 01:23:39UTC 재접근했지만 `accounts.google.com`, `Google Forms: Sign-in`으로 이동했다. 로그인/제출을 하지 않았다. 최신 본문 대조는 불가하므로 P1-009 AC4는 blocked로 남긴다. AGENTS.md의2026-09-24 요건과 평가4항목을 유지하며 미공개 배점/추가 필수 기술을 만들지 않는다.

## 키 없는 재현

```sh
uv sync --locked
uv run rfa demo --full
RFA_E2E_REPORT_DIR=/tmp/rfa-controlled-check .venv/bin/python -m pytest -q \
  tests/test_demo_e2e.py tests/e2e/test_fixture_pack.py \
  tests/e2e/test_rfa_scenarios.py tests/e2e/test_process_recovery.py tests/e2e/test_load.py
```

개발 task 상태는 canonical task YAML이 원본이다. 이 보고서는 실행 기록이며 task 운영 상태를 별도 수동 관리하지 않는다. 미검증 gate 때문에 전체 제품 완료를 선언하지 않으며, 문서만 바뀐 작업에 새 제품 테스트 결과를 꾸며 넣지 않는다.
