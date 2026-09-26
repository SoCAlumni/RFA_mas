# RFA 개발 작업 기록

상태 원본은 canonical `tasks/<ID>/task.yaml`이다. 이 문서는 사용자에게 전달하는
Git 추적 작업/오류 기록이며 task 상태를 대신하지 않는다. 검증의 상세 원본은
task별 `.agent/evidence/<ID>/<attempt>/`에 불변 저장한다. 비밀 값·인증 헤더·개인
원문은 어느 로그에도 넣지 않는다. 각 구현 커밋은 task ID와 검증 결과를 연결한다.

## 실행 방침 — 2026-09-26

- 사용자가 전체 task 개발, 모든 작업의 Git commit 및 로그 보존을 승인했다.
- P0 → P1 → 후순위 순서와 실제 dependency/owned scope를 지킨다. 기존 완료 기반을
  다시 만들지 않고, 외부 환경이 필요한 검증은 mock 성공으로 대체하지 않는다.
- 실패한 실행도 남긴다. 오류 로그 → 원인 가설/수정 → 새 attempt 순서로 진행한다.
  기존 운영 규칙의 수정/검증 cycle 최대 3회와 새 근거 없는 동일 실패 2회 제한을
  유지한다. 한도 도달 시 blocker/handoff를 남기고 독립 작업을 계속한다.
- 자체 프레임워크를 추가하지 않는다. LangGraph checkpointer, APScheduler,
  NVIDIA 공식 예제/Skill/NAT 등의 호환 버전·라이선스·실제 지원 경계를 확인한다.
- 최종 검증 원문: `RFA_E2E_Test_Scenarios_10_ko.md`. controlled API, real-model,
  real-integration, UI, 품질, 성능 gate를 분리한다. E2E-05 OpenShell 실제 격리와
  E2E-10 프로세스 중단/재개를 mock 단위 테스트로 통과 처리하지 않는다.
- NVIDIA 이외의 서비스는 로컬 self-hosting이다. 현재 mock/local 모드에서는
  선택적 HTTP endpoint가 실행되지 않아도 기반 개발을 진행할 수 있다.

## OPS-000 / baseline 준비 — 2026-09-26T06:12:51Z

- `git status --short`, `git rev-parse --verify HEAD`: Git은 초기화되었으나 HEAD는
  아직 없음. 이 진단 실패는 예상된 최초 상태이며 코드 오류나 재시도 성공이 아니다.
- 기존 소스/명세/사용자가 추가한 E2E 문서를 포함한 Git 후보 169개를 확인했다.
- 실제 `.env`는 열지 않았다. 후보 파일의 비밀 경로와 자격증명 패턴 검사를
  실행했고 탐지 0건, `.env.example`의 비밀 필드가 빈칸임을 확인했다.
  이는 알려진 패턴 검사이며 모든 종류의 비밀 부재를 수학적으로 보장하지 않는다.
- Git author 설정 유무 확인 완료. 값 자체는 기록하지 않았다.
- 디스크 여유 93GiB 확인. 이전 디스크 부족 blocker는 이번 개발 환경에 적용되지 않는다.
- 다음 행동: 기존 파일을 보존하는 최초 커밋 → 깨끗한 baseline 등록 → 실제 운영
  검증 → P0-014 계약 구현. 제품 AC나 E2E의 성공은 아직 주장하지 않는다.
- 최초 `git diff --cached --check`는 기존 16개 파일의 EOF 빈 줄 때문에 exit 2였다.
  원문 archive/hash와 기존 handoff를 보존해야 하므로 임의 정리하지 않는다. 최초
  baseline에만 `core.whitespace=-blank-at-eof`로 해당 기존 경고를 제외해 검사하며,
  이후 새 변경에는 기본 whitespace 검사를 사용한다. 기능/보안 검증 완화가 아니다.

## OPS-000 / OPS-001 재검증 — 2026-09-26T06:18:09Z

- 최초 보존 커밋: `d51137a`. canonical main baseline 등록 및 P0-014/P0-027
  별도 source worktree 생성 완료. 두 worktree의 control digest 일치를 확인했다.
- 임시 worktree/baseline 테스트 3개 통과(1.23초).
- 깨끗한 환경의 전체 회귀: `env -i PATH="$PATH" LANG=en_US.UTF-8
  .venv/bin/python -m pytest -q` — 256 passed, 29.92초, skip/error 없음.
- `scripts/contract_baseline.py check`: 기존 schema/OpenAPI와 합성 fixture 9개 일치.
- taskctl validate: task 66개 및 generated view 유효. 기본 `git diff --check` 통과.
- 새 불변 evidence: OPS-000/git-baseline-01, OPS-001/git-baseline-regression-01.
  이전 디스크 실패는 삭제하지 않았고 확보된 디스크·등록된 Git이라는 변경 근거로
  새 검증을 수행했다. 제품 E2E·NAT·실제 provider 통합 검증은 별도 후속이다.
- 다음: P0-014 추적/평가 계약 구현, P0-027 NAT 호환성 spike.

## P0-027 — NAT optional compatibility — 2026-09-26T06:40:47Z

- 기능 커밋 `c6a764b`: NVIDIA 공식 v1.8.0 wrapper/metadata/example을 검토하고
  optional `nat` extra에 nvidia-nat-langchain 1.8.0을 고정했다. 기존 core pin 유지.
- 실제 설치 wrapper 검증 8 passed, extra 없는 독립 환경 core 검증 2 passed.
  합성 fake model이며 제품 NAT adapter/HITL/실제 NVIDIA 모델 호출 성공과 다르다.
- 초기 upstream source 경로 추정은 404; release tree로 실제 파일명을 확인해 수정.
  lint의 async path 호출·긴 줄 4건은 경로 계산 위치/포맷 수정 뒤 통과했다.
- feature worktree 전체 suite: 255 passed / 9 failed. 원인은 canonical root만
  허용하는 migration 검사 경로였다. 보호 규칙을 변경하지 않았다. main 통합 뒤
  같은 전체 suite는 264 passed(39.06초), skip/error 없음.
- evidence: nat-spike-01, nat-integrated-01. 1.1 계약 통합 후 영향을 재확인하여
  nat-contracts-02에서 8 passed(0.53초). 최신 필수 AC를 통합 완료 처리했다.
- `uv`의 inherited VIRTUAL_ENV 경고는 각 feature/temp venv를 명시 사용해 처리.
  실제 .env를 읽거나 키를 로그로 출력하지 않았다.

## P0-014 — 1.1 reference 계약 — 2026-09-26T06:44:00Z

- 기능 커밋 `a865fac`, main에 별도 merge commit. Pydantic additive 1.1 DTO 28개,
  ID/역할/선택 context/정책/승인/평가와 typed trace 계약, 생성 schema·7종 fixture.
  1.0 baseline은 원형 보존; 현재 1.0 wire 호환성과 새 1.1 schema를 별도 검사했다.
- 첫 contracts-01: 테스트 14/34개는 통과했지만 독립 검토에서 fixture 참조 누락,
  mutable draft의 stale hash 승인 허점, local sandbox 주장, 비유한 측정값을 재현했다.
  통과로 제출하지 않고 **failed**로 저장했다.
- 수정: 승인 비교 시 serialized DTO/hash 재검증, 모든 참조/상태/mode binding,
  local-template sandbox 차단, NaN/Infinity 거절 및 음성 회귀 사례 추가.
- contracts-02: 14 passed(0.03초) + 50 passed(0.56초). 독립 리뷰 64 passed.
  기존 계약 suite 13 passed. main 통합 재검증 14+50 passed;
  전체 canonical 회귀 **326 passed(31.25초)**, skip/error 없음.
- 초기 생성 경고는 typed DraftTarget으로 수정. lint 초기 69건(2 자동 수정), 포맷·
  export 정리 뒤 남은 test import 2건 수정, 세 번째 검사 통과. AC를 제거하지 않았다.
- RFA-EXTENDED 1.1 digest `59d6b352dc9c074f0387b2ec81d834ab50a030a37802ce8d7c5c4b85c057c007`
  발행. 28개 consumer를 자동 ready로 만들지 않고 개별 수락 대상으로 남겼다.
- 진단 중 존재하지 않는 scripts/tasklib/operations.py 검색은 exit 2였다.
  `rg --files`로 실제 cli.py/store.py를 확인했다. 제품 실행 재시도와 구분한다.
- 신규 API·durable 세션·실제 팀원 소비자·NAT 제품 adapter·최종 E2E는 아직 미완료.
  다음은 P0-015 사용자 소유 세션/조회/DB migration. local.py를 공유하는 trace
  작업은 동시에 수정하지 않는다. 운영 audit는 source 변화로 stale 표시되며
  최종 회귀 때 현재 baseline으로 재검증한다; 과거 성공 증거는 보존된다.

## P0-015 착수 / E2E 검증 연결 — 2026-09-26T06:54:51Z

- 별도 task/P0-015 worktree에서 세션 구현 착수. 코드 변경 전 조사로 typed
  SessionCreate/Message/Detail/RunRecord와 API/기존 graph 보안 회귀 파일이 필요함을
  확인했다. coordinator가 최소 계약/export scope를 승인하고 새 generation 3으로
  claim을 교체했다. 설치별 서버 생성 local-owner와 기본 membership 없음,
  실제 loopback peer만 키 없는 로컬 데모 허용; legacy owner 미상은 restricted.
- claim scope 갱신의 첫 recover는 지원하지 않는 --generation 인자로 실패했다.
  뒤따른 edit/claim도 보호 규칙으로 거절되었으며 원본 변경/파일 삭제는 없었다.
  parser를 확인하여 잘못된 옵션 제거, 각 exit code 확인 후에만 다음 명령을
  실행하도록 수정했다. 수정 재시도 1회에 성공. 제품 검증 시도와 구분한다.
- 사용자 원본 RFA_E2E_Test_Scenarios_10_ko.md를 보존하고 기존 P0-026에 연결했다.
  controlled 01~04/06~09, 실제 subprocess kill/restart 및 100요청 부하 10,
  20/1000문서·golden20·3회 반복·사전 threshold/기능/보안/품질/성능 분리를 명시.
- E2E-09 M01 피드백 기능의 P1-005B 선행을 추가했다. P1-007B에는 E2E-05의
  실제 sandbox allow/deny와 정상 endpoint/sentinel 확인을 보강했고, P1-009에는
  10개 전체/variant·API/UI·real별 evidence 표를 연결했다. 새 final task는 만들지 않음.
- P0-026의 검증 범위가 단일 데모보다 커져 예상 1h→4h(+3h)로 명시했다.
  기존 개발 추정 28.5h에 이를 더하면 직렬 핵심 추정은 31.5h이며 24h 완료를
  보장하지 않는다. 공통 local.py/DTO 수정은 병렬 금지, 검토/통합도 필요하다.
- 이 항목은 명세 연결만 검증했다. E2E 10개 실행, UI, 실제 모델/팀원/격리
  완료 주장은 아직 하지 않는다. scripts/taskctl.py validate 결과: 66 task 유효.

## 다음 구현의 공식 자료 조사 — 2026-09-26

- P0-016: 설치 LangGraph 1.2.12/checkpoint 4.2.0에 대해 PyPI의
  langgraph-checkpoint-sqlite 3.1.1은 checkpoint >=4.1,<5와 Python >=3.10을 요구한다.
  아직 설치·resolver·실행 검증하지 않은 후보다. [배포 metadata](https://pypi.org/pypi/langgraph-checkpoint-sqlite/3.1.1/json),
  [공식 persistence](https://docs.langchain.com/oss/python/langgraph/persistence),
  [interrupt 재개](https://docs.langchain.com/oss/python/langgraph/interrupts)를 확인했다.
  interrupt 이전 node 코드가 재실행될 수 있으므로 멱등 기록과 재인증을 분리 검증한다.
- 탐색 중 존재하지 않는 graph/builder.py 읽기는 exit 1이었다. `rg --files`로 실제
  application/graphs/supervisor.py·domain.py를 확인했다. reference 사이트 markdown
  응답은 web 도구가 읽지 못해 공식 문서와 배포 metadata를 사용했다.
- P1-003 후보는 공식 Skill의 nemo-retriever 26.8.1 CLI + 로컬 LanceDB + 명시적인
  NVIDIA hosted embedding이다. 일반 PC에 GPU/전체 Blueprint를 요구하지 않는다.
  기존 core/NAT와 urllib3/tokenizers pin이 달라 격리 CLI 환경에서 검증할 예정이다.
  [고정 Skill 원본](https://github.com/NVIDIA/skills/blob/46293cb1dcc3b6984f1569cf8d819a387d5a9916/skills/nemo-retriever/SKILL.md),
  [26.08.1 CLI](https://github.com/NVIDIA/NeMo-Retriever/blob/26.08.1/nemo_retriever/docs/cli/README.md).
  CLI evidence에는 RFA revision/ACL이 없으므로 신뢰된 index manifest와 매핑해야 한다.
  이 조사는 설치·Skill 실행·실제 embedding 성공 증거가 아니다.

## P0-015 독립 검토·중단·새 접근 — 2026-09-26T07:12Z

- sessions-01에서 1.1 요청을 1.0 graph가 거절한 오류를 service projection으로 수정.
  sessions-02는 자동 19 passed였지만 독립 리뷰에서 두 사용자의 동일 client key가
  downstream Runtime/Response에서 충돌함을 재현했다. DB뿐 아니라 graph 호출 키도
  신뢰된 owner로 namespace하여 두 사용자의 완료·호출 횟수 회귀를 추가했다.
- sessions-03 자동 20 passed / 보조 회귀 34 passed여도 AC4 전체는 실패로 보존했다.
  외부 body의 run_id를 전역 PK에 사용해 known/unused ID의 생성 응답이 달라지는
  존재 추측 경로를 재현했다. 조회 권한만 통과했다고 전체 격리를 선언하지 않는다.
- 3 cycles 뒤 작업을 중단·blocked 처리하고 WIP `8fcb6f6`에 미완료 코드를 보존했다.
  clean worktree·중단 프로세스·합성 로컬 부작용과 실패 로그를 검토한 후 generation 4의
  새 접근을 명시 승인했다: HTTP run_id 서버 발급, proxy 경유 시 인증 요구.
  같은 실패의 무근거 네 번째 재시도가 아니며 이전 3회 증거·횟수는 삭제하지 않는다.
- 기존 CLI의 block→recover는 claim baseline을 보존하지 않아 현재 main을 선택했다.
  WIP 보존 후 정상 main merge로 baseline 정합성을 회복한다(reset/stash 없음).
  OPS-002는 claim history 보존과 불명확 baseline 거절을 구현 중이다.
- 환경의 read-only 확인: Docker CLI는 있으나 로컬 Colima daemon 접속 실패,
  OpenShell/NemoClaw CLI는 PATH에 없다. 설치·기동·실제 sandbox 결과는 아직 없음.
  `docs/DEVELOPMENT.md` 탐색은 파일 없음으로 종료했고 `rg --files docs`로 실제
  문서 목록을 확인했다. 이 진단은 제품 테스트 실패나 remote 서비스 요구가 아니다.

## P0-015 / OPS-002 통합 — 2026-09-26T07:25Z

- 세션 WIP `8fcb6f6` → HTTP 보강 `4549651` → reviewed baseline 정상 merge
  `dc3c595`를 main에 통합했다. 서버 run ID·사용자별 downstream 멱등 키·proxy 인증,
  원자적 세션/Task/run 관계와 owner 미상 legacy 자료 보존을 구현했다.
- sessions-http-01: 26 passed(1.23초), 기존 회귀 34 passed(0.62초).
  통합 `sessions-integrated-01`: 26 passed(1.40초). 기존 route-absence assertion은
  `2f50bb4`에서 실제 SessionRecord/SessionDetail/RunRecord 응답 schema 검사로 교체.
- OPS-002 `bd1e340`: integrated stale task의 명시적 clean-baseline 재검증,
  이전 claim/증거 보존, 미통합 baseline 소실 거절, canonical migration 감사 수정.
  worker 66+17 passed; current target 66 passed(47.18초)+17 passed(5.00초).
- 첫 OPS 통합 검사 중 P0-015 merge로 source가 바뀌어 evidence 등록이 거절됐다.
  66/17 자동 통과 관찰은 invalidated.json에 stale로 보존했다. target을 고정한
  새 capture로 재검증하여 완료했다. source 변경 전 검증을 새 commit에 쓰지 않았다.
- P0-015 최초 integration capture는 미발행 contract digest 때문에 거절됐다.
  root에서 frozen/extended schema·fixture와 50개 계약 테스트를 확인한 뒤
  1.1 digest `3a6d70524d532866780288d2105c49f133fdd2a57e3309c783dd8a26a2aed3c3`
  발행 후 capture/검증했다. 미지원 상대 API가 확정되었다는 의미는 아니다.
- 안정된 main `2f50bb4`의 전체 offline 회귀: **363 passed(57.46초)**.
  key 없는 기본 환경·설치 NAT fake-provider만 포함하며 실제 NVIDIA/전체 E2E는 별도다.
- P0-014/P0-027은 기존 구현을 다시 쓰지 않고 신규 안전 recovery로 현재 source를
  재검증했다. 계약 14+50 passed, NAT 8 passed, 별도 NAT 미설치 core 2 passed.
  최초/실패/과거 통합 증거와 source commit을 모두 유지했다.
- 보고서 orchestration의 한 JS 구문 오류는 파일/상태 변경 전에 발생했다.
  배열 구성을 분리한 한 번의 수정으로 해결했다. worker submit의 handoff 인자
  누락도 parser를 확인해 한 번 수정했으며 제품 성공 숫자에 포함하지 않는다.
- 다음: P0-016 공식 async SQLite checkpointer/인증된 재조회 resume와 P0-018
  순수 팀 selector를 별도 worktree에 배정. 선택적 NAT/관측 경로를 필수화하지 않는다.

## P0-018 통합 / P0-016 범위 인계 — 2026-09-26

- P0-018 `6973cfe`의 순수 TeamSelector·승인 template 2개·테스트를 main에 통합했다.
  worker V1 53 passed, 통합 V1 53 passed(0.05초), 기존 계약 회귀 64 passed(0.57초),
  extended fixture 7개 검사가 통과했다. selector-01 / selector-integrated-01 증거 보존.
- 사전 독립 검토의 정수 예산 bool/string/float coercion, 인증 bool coercion,
  registry 생성자의 승인 pin 우회, 큰 timeout OverflowError를 수정하고 음성 회귀를
  추가했다. 선택 결과가 runtime 권한을 부여하지 않으며 실제 팀 실행은 P0-019/020이다.
  TeamFactory는 실행 예산을 적용하고 최신 권한·runtime·승인 template를 다시 확인한다.
- P0-016은 테스트 helper 추가 scope가 필요해 미검증 WIP `d50de92`를 보존했다.
  old 프로세스 중단·clean worktree를 확인한 뒤 이전 claim을 fence하고
  `tests/test_supervisor_boundaries.py`만 추가 승인했다. 새 `task/P0-016-r2`에
  WIP를 정상 cherry-pick(`28ffd58`)했고 generation 3으로 계속한다.
  이전 worktree/commit/기록은 삭제하지 않았다. 아직 제품 검증 cycle은 시작 전이다.
- 새 resume 환경의 `uv sync --locked`는 성공했다. SQLite saver 설치를 재시작
  복구·승인 검증 완료로 표현하지 않는다. 전체 E2E/실제 NVIDIA·OpenShell gate는 미실행이다.

## 평가/trace 후속 조사 — 2026-09-26

- 기존 ID를 확인했다: P1-006D(안전 trace) → P1-006(Persona 12/행동 verifier)
  → P1-006E(공격 회귀), 후속 Persona 24개는 P1-006B이다. 없는 P0-029/030을
  생성하거나 기존 평가 task를 중복하지 않았다.
- 실제 legacy service는 임의 metadata/caller ID를 LocalJsonlTrace.emit에 전달한다.
  typed exporter만 추가해서 이 경로를 남기면 canary AC가 충족되지 않으므로
  P1-006D coordinator scope에 service/API·기존 security 회귀·관찰 계약을 추가했다.
  현재는 draft이며 제품 기능/검증 성공 주장이 아니다. P0-016과 공통 파일 충돌로
  직렬 실행한다. P1-006 evaluator는 이 관찰 계약 이후 별도 파일에서 개발한다.
- 현재 evaluator는 답변/evidence만 검사하고 실제 write/outbound ledger를 보지 않는다.
  정상 deny·Judge 오류/disabled·미수집을 pass로 처리하지 않는 verifier가 필요하다.
  공개 게시 시뮬레이션은 test-only sink에서 관찰하고 P0 실제 write 금지를 유지한다.
- 명세/파생 view 검증: taskctl validate에서 67 task와 current views 확인.
  전체 task의 제품 AC나 E2E 실행 결과를 의미하지 않는다.

## 재개 사전 오류 / 계약 갱신 후속 보수 — 2026-09-26T07:57Z

- 독립 probe에서 LangGraph 1.2.12의 checkpoint 없는 하위 graph가 부모 sync
  durability를 상속할 때 `_put_checkpoint_fut` AttributeError를 재현했다.
  부모 sync를 유지하고 하위 호출은 exit를 명시한 수정 후 정상 pending/재조회/
  중복 wakeup을 확인했다. 이 probe는 정식 P0-016 V1을 대신하지 않는다.
- 초기 DB 두 startup이 겹칠 때 seed marker가 빈 설치를 기존 것으로 오인할 수 있어
  initialize의 기존 schema 판독·DDL·migration을 한 BEGIN IMMEDIATE에 넣었다.
  동시 초기화 및 삭제 보존은 P0-016 회귀로 검증한다.
- OPS-002 후속: 이미 통합된 consumer가 stale/reserved가 되면 publish-contract와
  baseline 수락이 막히는 분기를 확인했다. 기존 성공·실패 evidence를 approaches에
  보존하고 같은 cli/tests/docs 범위에서 AC5를 추가해 generation 3을 배정했다.
  활성 claim·미통합 submission은 계속 거절하고 실제 통합 이력 확인·재검증 gate는
  유지한다. 새로운 관리 framework나 상태 우회 도구는 만들지 않는다.
- 로컬 read-only readiness: Colima CLI는 있으나 실행 중이 아니고 디스크 가용량은
  91GiB이다. 서비스 기동/설치/실제 OpenShell 검증은 아직 수행하지 않았다.
  [NemoClaw 공식 platform matrix](https://docs.nvidia.com/nemoclaw/latest/user-guide/openclaw/reference/platform-support)는
  Apple Silicon + Colima/Docker Desktop을 제한 있는 검증 경로로 분류하며 임의
  LangGraph 앱 자동 지원은 명시하지 않는다. 후속 최소 시연은 지원 runtime의
  제한된 MCP/API consumer로 구성하고 앱 backend 밖의 격리 경계를 따로 표시한다.
  [OpenShell middleware 공식 경계](https://docs.nvidia.com/openshell/extensibility/supervisor-middleware)는
  네트워크 정책 후의 콘텐츠 검사이며 tls-skip/일부 응답·WebSocket 한계가 있다.
  middleware 운영 구현은 다영 모듈의 범위로 유지한다.

## P0-016 worker 제출 / KB 인계 — 2026-09-26

- P0-016 최종 worker commit `3700de1` 제출: resume-01에서 22 passed(1.22초),
  session/API/graph/계약/settings 등 관련 회귀 164 passed(2.73초), 양쪽 schema/
  fixture 및 Ruff/diff 검사 통과. 실제 main 통합·계약 발행·target 검증은 대기 중이다.
  SQLite/WAL/SHM 접근 모드 0600, 실제 새 process resume와 재제출 금지를 검사했다.
- checkpoint 없는 child의 durability 경고는 숨기지 않았다. worker 중간 노드 복구와
  tool crash exactly-once는 이 task의 증거가 아니며 P0-021/026에 남는다. 기본
  in-memory 승인 mock은 재시작 후 승인 정보를 추정하지 않고 pending을 유지한다.
- KB 조사 결과를 P1-001/P1-001A 원본에 인계했다. source namespace·immutable
  revision·명시 current head/CAS·부분 export receipt와 실제 seed-once 재사용,
  현재 ACL 회수 뒤 과거 Run/Session 결과 재노출 차단의 구현 경로를 scope에 반영했다.
  현재는 여전히 draft/not_run이며 아직 사용 가능한 KB API로 표시하지 않는다.
- OPS-002 사전 독립 검토는 v2→v3 연속 계약 발행 뒤 자동 notice가 누적되면
  최신 수락도 막히는 추가 경계를 재현했다. 발행 이력에 기록된 동일 계약 notice만
  처리하는 최소 수정과 음성 검증을 진행한다. 무관 blocker 삭제는 허용하지 않는다.

## OPS-002 검증 1회 실패 / 설정 preflight — 2026-09-26T08:10Z

- contract-recovery-01 자동 결과는 control 92 passed(99.69초), migration
  17 passed(10.86초)였지만 AC1은 실패로 봉인했다. 별도 실제 임시 Git fixture에서
  동일 source를 소유했던 두 완료 task가 함께 stale/reserved가 되면 양쪽 recovery가
  서로를 막는 사례를 재현했다. 자동 통과 숫자를 근거로 전체 AC를 통과 처리하지 않았다.
- 명시 integrated_revalidation만, 상대가 claim 없이 stale이고 실제 과거 통합 증거가
  유효할 때 예약을 보존하며 첫 재검증을 허용하도록 고친다. 첫 claim 뒤에는 두 번째를
  거절해 직렬성을 유지한다. active/expired claim·미통합 제출·위조 이력은 계속 거절.
  실패 증거를 보존하고 최대 3 cycles 내 두 번째 정식 검증을 진행한다.
- P0-017 read-only 합성 probe는 doctor의 ready=true와 bootstrap 실패 차이를
  PostgreSQL URL 및 비-loopback HTTP 설정에서 재현했다. 실제 env나 네트워크를
  읽거나 변경하지 않았다. 설정/구현 가능성 검사를 공유하고 live probe not_run과
  local readiness를 분리하도록 원본 scope에 반영했다. 비밀 예제 값 9개는 빈칸이다.
- dev_env의 로컬 생성 토큰은 상대 서비스에 등록된 credential이 아니다. 이번 후속
  설정 task는 기존 실제 env 프로필을 수정하지 않으며, 예약된 scheduler/NAT/retention
  설정을 실제 기능이 완성된 것처럼 표시하지 않는다.

## OPS-002 / P0-016 대상 브랜치 통합 — 2026-09-26

- OPS-002 두 번째 정식 시도 contract-recovery-02는 control 94 passed(112.01초),
  migration 17 passed(11.99초). 첫 실패 기록을 유지했다. feature 6f39c69를 a94e9fa로
  통합하고 target에서 94 passed(113.73초), 17 passed(12.28초)를 확인해 close했다.
  비활성 과거 통합 예약의 직렬 재검증만 허용하며 활성/만료 claim과 미통합 제출은 거절한다.
- P0-016 feature 3700de1을 2a55de0로 통합했다. 실제 schema/fixture 검사 후
  RFA-EXTENDED 1.1 digest 0c285d7389dd2cd21676a887aae810e6a34b979c0b4b3a96f6831c8137fd8a65를
  발행했다. frozen/extended fixtures 9/7개와 계약·trace 회귀 64 passed(0.57초).
  target resume 22 passed(1.25초), 관련 회귀 164 passed(2.53초)를 확인하고 close했다.
  child durability의 알려진 경고는 유지했으며 외부 승인 원본·전체 E2E 성공 주장은 아니다.
- 변경된 공통 schema/settings/lock에 따른 과거 task의 검증 stale은 구현 삭제가 아니다.
  과거 evidence를 보존하고 필요한 선행 계약·세션·selector·NAT spike를 순서대로 재검증한다.
  공통 context의 초기 미구현 snapshot을 최신 원본 참조로 바꿔 향후 진행 갱신마다
  공통 설계 digest가 불필요하게 바뀌지 않게 했다. 이번 context 변경 자체도 재검증에 포함한다.

## 4f808b0 회귀·동시 초기화 실패·후속 설정 명세 — 2026-09-26

- 고정 main 4f808b0에서 `.venv/bin/python -m pytest -q`는 466 passed,
  46 warnings, 131.03초였다. known child durability 경고이며 full E2E/실연동 증거가 아니다.
- P0-014는 별도 worker 14/50 passed(0.01/0.59초), target 14/50 passed(0.01/0.57초).
  P0-015는 worker 26+API4 passed(1.34/0.21초), target 26+API4 passed(1.31/0.22초).
  P0-018은 새 계약 digest 수락 후 worker 53+64 passed(0.07/0.58초),
  target 53+64 passed(0.06/0.58초), frozen/extended 검사 통과. 과거 evidence는 보존했다.
- 이어 P0-016 stable-main-worker-01은 1 failed, 21 passed(1.24초)였다.
  동시 최초 startup이 `_connect`의 `PRAGMA journal_mode=WAL`에서 database is locked로
  실패했다. 전체 테스트의 이전 성공으로 이 실패를 숨기지 않았다. 제어된 DELETE-mode
  DB probe는 writer 유지 중 기본 5초 busy timeout에도 0.08ms의 즉시 SQLITE_BUSY를
  재현했다. [SQLite busy handler](https://sqlite.org/c3ref/busy_handler.html)와
  [WAL 영속성](https://sqlite.org/wal.html#persistence_of_wal_mode)을 근거로 일반 연결의
  WAL 전환을 제거하고 repository/checkpoint setup만 제한된 같은-host lock으로
  직렬화하는 보수를 승인했다. business SQL/tool write 재실행은 하지 않는다.
  최초 실패 evidence를 봉인했고 수정 소스의 두 번째 정식 검증을 진행 중이다.
- P0-017은 active 설정/example parity와 단일 planned catalog를 구분했다. 실제 읽지
  않는 scheduler/예산/retention을 지원 설정으로 광고하지 않는다. P0-019는 기존 Task
  owner registry·승인 template 보존, 별도 실행 예산·member lifecycle/unknown cleanup
  예약을 명시했다. P1-006D는 실제 service/API trace 경계와 로컬 retention 소비를 포함한다.
- 조사된 작업량에 맞춰 추정을 P0-017 0.5→1h, P0-019 1.5→2h, P1-006D 0.5→2h로
  변경했다. 최초 31.5h 이력은 보존하며 현재 직렬 추정은 34h(D4 8h 별도)다.
  이에 이관 테스트의 옛 고정 합계 두 곳이 실제 진단에서 2 failed(0.65초)로 확인됐다:
  D1 8.5≠8, 기술 증가분 6≠4.5(D2도 8.5). 기존 OPS-002의 schedule 회귀 범위에서
  승인된 새 합계와 변경 이유만 반영한다. 제품 보안/기능 AC를 낮추는 변경은 아니다.

## SQLite 패치 런타임 격리 qualification — 2026-09-26

- 현재 공유 Python3.12.13은 SQLite3.50.4였다. 이는 위 startup 오류와 별개인
  [공식 WAL-reset 공지](https://sqlite.org/wal.html#the_wal_reset_bug)의 수정 전 범위다.
  공식 PBS20260807의 동일 CPython3.12.13 arm64 artifact를 임시 디렉터리에 설치했다.
  SHA256은 25baa97c65b3f0aa90e21131b4f9e80aef8899e8144006db8a9d2c1ab9e807e3과 일치했다.
  실제 내장 SQLite3.53.1을 확인했다. 공유 interpreter/기존 venv는 아직 바꾸지 않았다.
- 새 임시 DB에서 spawn 3프로세스×50 commit, PASSIVE/TRUNCATE checkpoint, 150행,
  재오픈 WAL 및 전후 integrity_check=ok. 실제 AsyncSqliteSaver3.1.1 저장/새 saver 조회도
  확인했다. rare bug 자체 재현 또는 모든 제품 AC 보장으로 확대하지 않는다.
- 격리 venv에서 현재 exact lock + NAT extra 설치 후 기존 NAT spike 8 passed(9.24초).
  pyproject/lock/.python-version 변경 없음, retry 없음. 상세 재현 명령/공식 출처는
  `/private/tmp/rfa-sqlite-qualification.IKbCas/QUALIFICATION.md`에 보존했다.
  root 환경 전환·전환 후 전체 회귀는 아직 미실행이며 활성 worker 검증 중 전환하지 않는다.
  임시 경로가 만료되어도 실제 명령/출처/결과를 보존하도록 검토한 원문 보고서를
  [환경 qualification 기록](ENVIRONMENT_QUALIFICATION.md)에 추가했다.

## WAL 보수·선행 재검증·일정 회귀 통합 — 2026-09-26

- P0-016의 setup-only lock 보수 4bac516을 main 53d7068에 통합했다. 첫 실패를
  보존한 두 번째 시도는 worker 31+164 passed(2.35/2.58초), 독립 target
  31+164 passed(2.26/2.45초)였다. warning 17/26개는 그대로 기록했다.
  async setup 실패·취소 시 connection drain/close 뒤 lock을 해제한다. 실제 두
  프로세스 충돌·symlink/FIFO·timeout·취소 회귀가 포함되며 tool write 재시도는 없다.
- 변경 영향을 받은 P0-015는 worker 26+4 passed(1.47/0.19초), target
  26+4 passed(1.34/0.17초)로 재검증했다. P0-027은 설치된 NAT 경로 worker/target
  각각 8 passed(0.42/0.43초), NAT 없는 별도 기본 환경은 2 passed(0.08/0.05초).
  root에 NAT가 없다는 초기 추정은 실제 모듈 조회로 정정했다. 없는 테스트/읽기 경로
  조회는 성공으로 처리하지 않았으며 정확한 경로·환경과 정정은 handoff/evidence에 남겼다.
- OPS-002의 a8d2888은 이관 테스트와 설명 문서만 변경해 ff3c8288로 통합했다.
  현재 34h와 초기 31.5h 이력을 분리하며 원문 checksum/ID/완료 이력/최종 gate는
  유지했다. worker control 94 passed(117.50초), migration 17 passed(13.02초);
  target 94 passed(119.73초), 17 passed(14.97초), skip=0이었다.
  V1은 이전 112초 실측에 따라 명세 timeout을 180초로 명시하고 실제 적용했다.
  target result를 쓰기 전 한 차례 조회한 missing 파일은 검증 성공 근거가 아니다.
- 이 배치의 P0-014/015/016/018/027은 통합된 범위의 done이다. 전체 제품 E2E,
  실제 NVIDIA ModelPort/Skill, OpenShell/NemoClaw, 실제 승인·게시 성공은 아직 아니다.
  변경 원본과 신규 worker/target 증거를 별도 보존 commit으로 관리한다.

## 패치 Python의 보존형 환경 전환 시작 — 2026-09-26

- PBS20260807 CPython3.12.13/SQLite3.53.1을 `.local/toolchains/`에 별도 설치했다.
  공식 pinned metadata와 앞서 확인한 artifact SHA256을 사용했으며 공유 Python,
  PATH, `.python-version`, pyproject/lock 및 다른 worktree venv는 변경하지 않았다.
  설치 완료를 기다리지 않은 최초 version 조회는 missing executable 오류였고,
  설치 종료(exit0, 17.20초) 후 실제 버전 assertion을 확인했다. 설치 재시도는 없었다.
- 모든 root 검증 프로세스가 종료된 뒤 기존 `.venv`를 삭제하지 않고
  `.local/retained-envs/root-before-pbs20260807`로 이동했다. 새 `.venv`는 명시적인
  durable interpreter와 기존 exact lock + NAT extra로 생성한다. 실제 env 파일은 읽지
  않았다. 이전 환경의 Python 자체는 보존되지만 entrypoint shebang은 옛 위치이므로
  복구 시 원래 `.venv` 위치를 복원해야 한다. 자동/활성 worker 전환은 하지 않는다.
  전환 후 실제 버전과 전체 회귀 결과는 다음 기록에서 별도로 확인한다.

## 패치 환경 실제 회귀·다음 구현 시작 — 2026-09-26

- 새 root `.venv`는 exact lock의 168개 패키지를 설치했고 CPython3.12.13,
  SQLite3.53.1, NAT langchain1.8.0의 실제 값/assertion을 확인했다.
  고정 source main db49353에서 `.venv/bin/python -m pytest -q`를 실제 subprocess
  timeout=300초로 실행한 결과 **475 passed, 46 warnings, 160.05초**였다.
  수집된 테스트 skip/fail은 없었다. 경고는 기존 checkpoint 없는 child graph의
  durability 경고이며 전체 제품 E2E/실제 외부 연동 통과를 뜻하지 않는다.
- P0-017은 db49353의 별도 worktree/claim으로 구현을 시작했다. root/다른 worker의
  환경을 변경하지 않으며 feature도 명시적인 패치 interpreter를 사용한다.
- P1-001의 최신 계약을 provider로 수락하고 ready 명세를 정리했다. immutable
  revision/CAS/행별 receipt, current/nondeleted projection, private DB file,
  seed/legacy 보존과 기존 재개 보안 fixture의 정당한 revision 변경을 연결했다.
  제품 구현·검증은 아직 not_run이다. shared DTO/composition 변경은 직렬 처리한다.
- P0-028은 설치된 NAT1.8.0 평가 소스/registry/config 형식을 추가 조사했다.
  CLI entrypoint는 load_dotenv를 자동 호출하므로 programmatic EvaluationRun을
  사용하고 write_output=false와 output=null을 모두 적용한다. 첫 직접 Config
  검증은 plugin discriminator 오류였고 공식 load_config의 discovery 순서로
  바꾼 합성 schema probe가 통과했다. 제품 workflow 실행은 하지 않았다.
  evaluator 예외 누락·미관측 token의 기본0·profiler 미설치 한계를 명세에 보존했다.
  P1-006D 실제 관측 계약 수신 전 draft이며 로컬 대체 성공을 NAT 성공으로 세지 않는다.

## P0-017 설정/doctor 구현·실패 보수·통합 — 2026-09-26

- feature 9cd1c44를 main 518a6632에 통합했다. settings/example·bootstrap 공통
  preflight·doctor·검증 5파일만 수정했다. 실제 env, public DTO, API readyz,
  dev_env credential 생성 로직, dependency/lock은 변경하지 않았다.
- doctor는 configured/implementation과 lifecycle/provider probe not_run을 분리한다.
  미지원 real provider/NAT/scheduler/egress 선택은 client 생성 전에 명시 오류다.
  key/flag는 권한이 아니며 예제 secret 9개는 빈칸이다. planned 설정은 단일 catalog로
  관리하고 아직 소비하지 않는 변수를 활성 설정으로 나열하지 않는다. SQLite 버전과
  fixed/affected/unknown은 진단이지 integrity/실행 성공 증거가 아니다.
- settings-01은 **1 failed, 54 passed(0.31초)**였다. httpx가 닫히지 않은 대괄호
  URL을 hostname으로 받아 malformed 설정을 non-loopback 미지원으로 분류했다.
  요청은 차단됐으나 오류 분류 AC가 실패했다. 원본 증거를 보존하고 stdlib URL
  구조/port/hostname 검사를 추가했으며 동일 실패를 그대로 재시도하지 않았다.
- settings-02는 설정 56 passed(0.27초), 관련 154 passed(3.83초), 계약 fixture
  9+7개 및 Ruff/diff 검사를 통과했다. 별도 target은 56 passed(0.20초),
  154 passed(3.90초), 32개 known durability warnings와 계약/Ruff 통과였다.
  feature는 실제 NAT 미설치, target은 NAT1.8.0 설치 환경이며 둘 다 SQLite3.53.1.
  제품 NAT/실제 공급자 통과와 혼동하지 않는다. 최대3회 이내 두 번째 수정 검증에 성공했다.
- 변경된 설정의 영향을 받는 선행 source evidence는 필요한 범위만 재검증한다.
  P1-006D의 durable safe alias/순번, 실제 포트와 sink 관측 구분, 없는 승인 원본 ID의
  null 처리, 오류 projection, trace TTL과 run metadata 분리를 구현 명세에 확정했다.
  이 trace 기능은 아직 not_run이며 다음 shared DTO/composition 작업으로 진행한다.
- 앞선 후속 명세 변경 후 이관 회귀는 별도로 17 passed(14.36초)였다.

## 설정 통합 후 재검증·추적 구현 전 점검 — 2026-09-26

- source main `715db79142da1456a2fb1aae208d107d35b53a76`을 고정하고 필요한 범위만
  worker/target에서 각각 재검증했다. P0-014는 14+50 passed, P0-015는 26+API4
  passed, P0-016은 31+관련201 passed, P0-018은 53+계약64 passed,
  P0-027은 실제 NAT 설치8+실제 NAT 미설치2 passed였다. 각 새
  `post-settings-worker-01`/`post-settings-target-01` source/result를 보존한다.
  frozen/extended fixture도 9+7개 valid다. 기존 durability 경고는 제거하거나
  성공 숫자로 합치지 않았다. root Ruff 검사도 통과했다.
- P0-016 worker와 모든 target은 SQLite3.53.1이며 다른 worker의 기존3.50.4
  환경 차이는 증거에 남겼다. P0-027 미설치 검사는 실제 별도 no-NAT 환경에서
  현재 source를 사용했다. 사전 package metadata 조회의 `nvidia-nat` 이름 오류는
  `nvidia-nat-core`로 수정했고 handoff에 보존했다. 제품 테스트 실패는 없었다.
  taskctl validate는 67개 원본/파생 view 정합성을 확인했다.
- 추적 구현 전에 설치된 LangChain/LangSmith 소스를 확인했다. env-i의 합성
  tracing flag/key와 fake tracer constructor만 사용한 첫 probe는 함수 내부 import를
  모듈 속성으로 patch하려다 AttributeError였다. 실제 import 위치를 확인한 두 번째
  probe는 guard 밖 constructor1회, public `tracing_context(enabled=False, parent=False)`
  안0회를 관찰했다. 실제 tracer 생성·network·제품 workflow는 실행하지 않았다.
  이 SDK probe는 현재 제품의 유출 또는 제품 guard 구현 성공 증거가 아니다.
- P1-006D에 native run/resume 환경변수 기반 tracing 차단과 API validation의
  attacker extra-key/loc canary 검사를 추가했다. 필요 시 이미 잠긴 LangSmith0.14.0을
  direct dependency로만 선언한다. 새 관측 서버/SDK upgrade는 범위 밖이다.
  공식 public API 근거: https://docs.langchain.com/langsmith/trace-without-env-vars
  제품 전체 E2E·실제 NVIDIA/Skill/OpenShell/NemoClaw gate는 여전히 미실행이다.
- 보존 commit 전 `git diff --check`는 CLI가 기록한 handoff 4개의 EOF 빈 줄을
  보고했다(exit2). 제품 source 오류는 아니며 완료된 운영 원본을 우회 편집하지 않고
  그대로 보존한다. YAML/schema/view 검증은 별도로 통과했다.

## 다음 provider/consumer 구현 경계 확인 — 2026-09-26

- P1-006D는 main56f289f의 별도 worktree에서 패치 Python/default lock으로 시작했다.
  coordinator 소유 task에 worker session으로 claim한 첫 요청은 권한 오류로 거절됐다.
  project 권한을 늘리지 않고 기존 coordinator 위임 session으로 독점 claim하도록
  정정했다. 제품 소스 권한 변경이나 claim 우회는 하지 않았다.
- P0-019 소스 점검에서 TeamInstance의 schema 검사만으로 owner/spec/budget/mode
  binding이 보장되지 않음을 확인했다. 실제 저장 요청과 정확 비교, 변조 응답의
  occupied/unknown 보존, 타인 응답 ID cleanup 금지, boot-time snapshot 대신
  호출 시점 권한 resolver를 기존 scope/AC에 보강했다. 제품 구현·검증은 아직 아니다.
- P0-028의 설치 소스는 empty telemetry와 write_output=false만으로 ambient
  LangSmith 또는 임의 callback의 raw 수신을 막지 않는다. EvaluationRun 전체 guard,
  adapter 소유 empty callback, CLI 미import를 명세에 추가했다. 독립 native/NAT
  DB의 alias 문자열이 아니라 run-observation 연결과 정책/본문 의미를 비교한다.
  이번 확인은 기존 설치 소스 읽기이며 workflow를 실행하거나 NAT 성공으로 세지 않았다.
- 읽기 전용 탐색 중 존재하지 않는 `scripts/tasklib/controller.py`, `graphs/core.py`,
  아직 생성 전인 feature `application/observations.py` 경로 조회가 각각 missing으로
  끝났다. `rg --files`와 실제 cli.py/supervisor.py 경로로 교정했고 존재하는 구현이나
  검증 결과로 기록하지 않았다. 동일 실패 명령의 반복 재시도는 하지 않았다.

## P1-006D 첫 구현·검증 checkpoint — 2026-09-26

- 별도 feature에서 durable alias/단조 sequence/typed 원장, 실제 포트 관측,
  native run/resume tracing guard, 오류 projection, 제한된 private JSONL exporter와
  파일 TTL을 구현했다. 기존 LangSmith0.14.0의 직접 의존성 선언 외 package 버전은
  바뀌지 않았다. 아직 main 통합이나 제품 E2E 완료는 아니다.
- 첫 formal `trace-01`에서 신규 V1 **11 passed(1.81초)**, 기존 V2는
  **200 passed / 2 failed(11.04초)**였다. 기존 `review_query_pending` code가
  allowlist에서 빠졌고, failure projection이 허용된 public partial draft까지
  제거한 것이 회귀 원인이다. 원래 테스트/AC를 낮추지 않고 안전한 nested projection과
  기존 code 의미를 유지하도록 수정한다. 실패 evidence는 같은 파일에 덮어쓰지 않는다.
- 코드 검토에서 runtime의 반환된 denied/timed_out/outcome_unknown 및 raised
  OutcomeUnknownError가 성공/일반 실패로 잘못 표시될 수 있는 경계를 확인했다.
  또한 요청에서 domain을 생략하면 라우팅 이후에도 source provenance를 놓칠 수 있다.
  이를 두 번째 수정 검증에 포함하며 실제 trusted 경계에서만 domain/상태를 연결한다.
- 구현 전 Ruff long-line 검사 실패는 22→2→0으로 보수했다는 worker 기록을 보존한다.
  이는 제품 pytest 통과 숫자가 아니다. root의 추가 읽기에서 존재하지 않는
  tests/test_cli.py와 생성 전 evidence 경로 조회는 missing으로 종료했으며, 실제 파일
  목록/worker 단계 보고로 정정했다. 이러한 조회를 제품 검증으로 세지 않는다.
- `trace-01`의 V1은 pytest 11개가 통과했어도 코드 검토에서 발견한 AC1/AC3
  미충족 때문에 evidence 판정을 failed로 기록했다. 테스트 숫자와 AC 판정을 분리했다.
  추가 독립 검토는 시작 event를 DB에 기록한 뒤 exporter가 실패할 경우 실제 provider
  호출 전인데 호출 수가 증가하는 문제를 찾았다. exporter 실패+inner spy를 추가해
  의도/미완료와 확인된 실제 호출을 구분하도록 같은 두 번째 cycle에 보수한다.
- 위 로그 보강의 첫 apply_patch는 문맥 줄 불일치로 원본 수정 없이 거절됐다.
  실제 마지막 줄을 확인해 두 번째 patch로 반영했으며 제품 검증 실패와 구분한다.
- 두 번째 `trace-02` 실제 결과는 V1 **19 passed / 2 failed(2.91초)**,
  V2 **199 passed / 3 failed(10.76초)**다. 저장 전에 nested draft correlation을
  정화하면서 immutable draft JSON과 충돌했다. 추가 두 신규 fixture 문제는 동일
  draft ID 재사용과 reference HTTP 합성 인증 설정 누락이었다. 이 실패도 원본 보존한다.
- 세 번째 cycle은 저장 원본의 draft/hash/version/target을 보존하고, 승인된 outward
  run/resume/get 및 API RunRecord/SessionDetail의 오류 결과에서만 nested projection을
  적용하는 접근으로 수정한다. 저장 error 자유 텍스트 정화는 유지하며 immutable 검사나
  기존 회귀 기대를 낮추지 않는다. 허가된 원본 대화 메시지는 error export와 구분한다.
  세 번째도 실패하면 같은 접근의 네 번째 재시도를 하지 않고 근거·blocker를 남긴다.

## P1-006D 통합·전체 회귀 차이 — 2026-09-26

- 세 번째 worker `trace-03`은 V1 **21 passed(3.24초)**, V2 **202 passed(11.15초)**,
  추가 graph/HTTP/policy 등 **75 passed(2.61초)**였다. 독립 검토 후 source6073519를
  main e634435에 merge했다. 원래 두 실패와 수정 근거를 보존하고 AC를 낮추지 않았다.
- frozen schema/OpenAPI9개·extended7개 fixture 실제 검사 후 additive1.1 digest
  `37bec7d97ee0a558c6890de82c0b1c463a1f0cb546d2cc778932a447c2f5383a`를 발행했다.
  최초 target begin은 미발행 digest 때문에 거절됐고 기존 publish-contract 절차로
  해결했다. 상대 서비스 지원 합의나 실제 연결 성공을 의미하지 않는다.
- root lock+NAT sync는169개 resolved, editable rfa-mas만 재설치했다. Python3.12.13,
  SQLite3.53.1을 유지한다. `trace-target-01`은 coordinator가 플러그인 자동 로딩을
  잘못 비활성화해 pytest-asyncio가 실행되지 않았다. V2는132 passed/29 failed/41 errors;
  V1도 setup/async 실행 오류였다. 제품 검증 통과로 처리하지 않았다. 첫 report의
  finish 시각 전사 오류는 원본을 덮어쓰지 않고 timestamp-erratum.json으로 정정했다.
- 정상 저장소 pytest 설정으로 실행한 `trace-target-02`는 **21 passed(3.30초)**,
  **202 passed(11.39초)**, skip/error0이다. Ruff 및 계약 검사도 통과했고 해당 한정
  target AC를 integrate/close했다. 알려진 child graph durability 경고는 그대로 기록한다.
- 별도 전체 suite는 **532 passed/1 failed(143.22초)**였다. 실패는 trace guard의
  positive-control 테스트이며 단독 실행 때와 달리 fake tracer가 생성되지 않았다.
  설치 LangSmith0.14.0 소스와 네트워크 없는 합성 probe에서 env lookup의 lru_cache가
  이전 false를 보존함을 확인했다: cache clear 전0회, 후1회 fake constructor.
  실제 guard 우회나 유출이 관찰된 것은 아니다. 전체 회귀/최종 제품 gate는 미통과다.
  다음 P0-019의 최소 후속 범위에서 해당 합성 환경 테스트의 cache 격리를 고치고
  양성/음성 assertion을 유지한 채 순서 회귀를 다시 검증한다. 기존 실패 evidence
  `P1-006D/full-regression-01.json`은 보존한다. 같은 구현 오류의 네 번째 반복이 아니다.
- 읽기 진단의 추정 경로 operations.py/nat_compat.py/test_nat_compat.py는 missing이었다.
  실제 cli.py와 tests/spikes/test_nat_compatibility.py, 설치 패키지 소스로 교정했다.
- 다음 팀 Factory는 bootstrap에서 실제 runtime 지원 descriptor를 받는다. 관찰 wrapper의
  prepare/cleanup 메서드 존재만으로 내부 HTTP 지원을 추정하지 않는다. lifecycle DB
  기록과 아직 미수집인 lifecycle trace를 분리한다. KB는 그 다음 공유 migration 작업이다.
  실제 NVIDIA/Skill/NAT 제품 경로/OpenShell/NemoClaw 및 최종10개 시나리오는 미실행이다.

## 추적 통합 후 기반 재검증·후속 구현 배정 — 2026-09-26

- main35f4340을 고정한 worker/target 재검증: P0-014 각각14+50 tests 및 계약9+7,
  P0-015 각각26+API4, P0-016 각각31+관련81, P0-017 각각56 및 계약9+7,
  P0-018 각각53+계약64 및 fixture9+7, P0-027 각각 NAT설치8+실제미설치2 통과.
  해당 `post-trace-worker-01`/`post-trace-target-01`의 source/result를 보존했다.
  제품 소스 변경이나 선행 구현 재작성 없이 영향 범위만 다시 확인했다.
- P0-014 첫 report는 선언 assertion 원문 대신 요약을 넣어 봉인 거절됐다.
  뒤의 state 명령도 stale revision으로 거절되어 보호되었다. 실제 assertion과
  최신 revision을 확인하고 제출했으며, capture 전에 실행한 target 검사는 공식
  증거로 재사용하지 않고 capture 이후 다시 실행했다. 이 과정의 patch 형식 오류도
  handoff/report에 보존한다. 테스트 실패를 은폐한 것이 아니라 운영 입력 오류다.
- P0-017 target 테스트 후 `nvidia-nat`라는 잘못된 distribution 조회가 실패했다.
  실제 선언 패키지 nvidia-nat-langchain/nvidia-nat-core 각각1.8.0을 확인했으며
  원래 결과를 덮어쓰지 않고 `post-trace-target-01-metadata-correction.json`을 남겼다.
  기본 worker와 NAT 설치 target의 차이를 실제 제품 NAT 검증과 혼동하지 않는다.
- P0-019는 runtime 지원 descriptor/DB lifecycle 관측 경계를 명세에 확정했고,
  앞선 전체 회귀의 합성-env cache 격리 수정만 tests/test_trace_contract.py에 추가
  소유 범위로 연결했다. 제품 tracing guard나 공통 conftest를 변경하는 작업이 아니다.
- P1-006 평가 명세는 실제 ledger API/spy와 합성 sink를 구분하고 필수 관측 누락을
  unknown/error로 유지하도록 보강했다. 기존24 ID와 새12사례 요구는 보존한다.
  Judge 미실행/오류·완료 assessment와 실제 호출 시도, 평가기 검증과 제품 gate를
  분리하며 isolated CLI는 Settings/dotenv 이전 분기다. 구체화한 남은 추정은2시간;
  제품 구현·12시나리오 성공이나 실제 Judge 통과는 아직 아니다.
- 위 평가 patch를 agent 작성 완료 전에 조회한 한 번의 missing-file 진단은 이후
  실제 작성·schema 검증된 파일로 교정했다. 존재하지 않던 산출물을 사용하지 않았다.
- P0-028은 baseline35f4340, task/P0-028, worker-nat generation1로 실제 구현에
  착수했다. 별도 source worktree와 patched Python/locked NAT extra를 사용한다.
  다음은 최신 baseline의 P0-019 팀 Factory이며 두 task의 수정 파일은 겹치지 않는다.
  전체 회귀의 추적 테스트 cache 문제와 최종10개 E2E/real gate는 여전히 미통과 상태다.
