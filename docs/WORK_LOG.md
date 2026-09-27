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

## P0-028 NAT 평가 adapter 통합 — 2026-09-26

- feature `fd6bc94`의 5개 소유 파일만 main에 통합했다. 공식 NAT1.8.0
  EvaluationRun + langgraph_wrapper 바깥에서 기존 WorkService를 한 번 호출하며,
  caller 소유 lifecycle/SQLite checkpoint/승인 대기 상태를 유지한다. 원문 대신
  허용된 ledger·상태만 NAT로 전달하고 ambient tracer·출력 callback을 차단한다.
- `nat-adapter-01`은 V1 21 passed/1 failed/1 teardown error, V2 2 passed였다.
  ResumeRequest에 잘못된 필드를 쓴 테스트와 실제 LangSmith client를 만든 양성 대조
  테스트가 원인이었다. 네트워크 guard가 접근 시도를 차단했으며 성공한 외부 전송은
  없었다. 실패 evidence를 보존하고 실제 event_id와 client 없는 fake tracer로 수정했다.
- 두 번째 worker cycle은 **22+2 passed**, 추가 실제 NAT 미설치 환경3개 및 관련
  계약/trace DTO/resume95개 통과. root 독립 `nat-target-01`은 **22 passed(4.97초)**,
  **2 passed(1.10초)**, skip/error0이었다. target source/result와 실패/성공 worker
  evidence를 별도로 보존하고 generation1/revision25에서 integrate/close했다.
- 단일 신뢰된 합성 harness가 범위다. 과거 run을 새 실행 증거로 재사용하지 않는다.
  Tool/publish/internal_nodes는 uncollected, provider token은 null, Judge 품질은
  not_run이다. NAT streaming/HITL resume·NVIDIA 실호출·팀원 실제 연결·최종 E2E
  성공을 주장하지 않는다. 기본 Settings의 NAT flag는 여전히 예약 설정이다.
- P0-019 handoff를 작성 전에 조회한 진단은 missing이었으며 완료 기록으로 사용하지
  않았다. 해당 Factory 구현과 tracing-cache 순서 회귀 수정은 다음 통합 단위다.

## P0-019 영속 팀 Factory 통합·전체 회귀 — 2026-09-26

- feature `0f2cc17`, main 통합 `b5c825e`: migration3에 Task 상세·owner별 생성 key·
  Task당 팀 슬롯·멤버별 준비/정리·generation CAS 기록을 추가했다. Runtime IO는
  DB transaction 밖이며 unknown/부분 실패/정리 실패에 새 팀을 자동 생성하지 않는다.
  Factory는 매 요청 현재 승인 pin/권한/예산을 다시 확인하고 raw RuntimePort와 명시적
  지원 descriptor를 사용한다. 응답의 spec/mode/member/resource 참조 변형도 거절한다.
- 기존1.0은 보존하고1.1 typed lifecycle을 발행했다. 새 digest는
  `32b512c810ce23b8d0d9a277c7d11fd24249dabfe3dff2fbbcfd7e460c71cc77`이며
  실제 schema와 fixture9+7 검사 근거를 teams-publication-01.json에 남겼다.
  팀원 API 합의·OpenShell 운영 또는 역할별 graph 실행 완료를 의미하지 않는다.
- 첫 worker cycle은 **38+182 passed**, 추가 회귀122 passed였다. root 독립
  `teams-target-01`도 **38 passed(0.43초)+182 passed(8.80초)**, skip/error0으로
  통과해 generation1/revision20에서 통합·close했다. 첫 lint42개(대부분 format/import)
  및 잔여2개는 정식 검증 전 수정했고 실제 실패/진행 과정은 handoff에 남겼다.
- 이어 실행한 전체 suite는 **596 passed/1 failed,77 warnings(151.27초)**였다.
  실패는 tests/test_task_migration.py:90의 D3 예상11h와 실제12h 차이다. 앞선
  P1-006 조사에서1→2h로 늘린 명세를 일정 회귀 기대값에 반영하지 못했다.
  이전 tracing-cache 실패는 이 전체 실행에서 재현되지 않았다. 전체 통과로
  기록하지 않고 full-regression-01.json을 보존한 채 기존 OPS-002에서 보수한다.
- 다음 역할 실행 P0-020은 실제 service/Run projection과 observation actor 경계를
  조사해 누락된 durable Task/Team 결합·역할 관측·취소 파일 범위를 추가했다.
  기존 AC/선행 작업은 유지하고 추정1.5→3h, 아직 draft/not_run이다. 따라서 현재
  D1=8.5h/D2=10h/D3=12h, 기술 추가6h 포함 직렬36.5h다. 최초31.5h와 중간34h는
  역사적 추정이며 24시간 완료를 주장하지 않는다. 일정 변경 때문에 보안/최종 gate를 줄이지 않는다.
- NVIDIA adapter도 공식 hosted 모델 reference와 local NIM 예제를 구분해 명세를
  보강했다. httpx 재사용, 허가된 endpoint·남은 예산·안전한 오류·명시적 mode를
  소비하고 tool loop는 만들지 않는다. 미정인 egress/예산 계약을 draft로 명시했다.
  named/auto timeout을 미지원으로 단정하지 않으며 실제 제품 경유 호출은 P1-002A다.
  [Lightning hosted API](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-5-lightning-30b-a3b-infer),
  [local NIM 예제](https://docs.nvidia.com/nim/large-language-models/2.0.10/get-started/advanced/get-started-nemotron-3.5-lightning.html)를 연결했다.
- 준비 과정의 잘못된 docs/contract_baseline.py 및 adapters/observations.py 경로는
  실제 scripts/contract_baseline.py·application/observations.py로 수정했다.
  P1-002 첫 edit-spec은 오래된 expected revision1로 거절됐고 최신2 확인 후 반영했다.
  OPS-002 patch 검증 준비의 unsupported structuredClone·read_input 반환형 오인은
  실제 오류에 따라 JSON clone/YAML parsing으로 수정해 세 번째 검증에 성공했다.
  이 준비 진단을 제품 테스트나 실제 공급자 검증으로 집계하지 않는다.
- 새 계약의 consumer 수락과 YAML67개 정합성 검사를 수행했다. 관련 기반은 새 source
  재검증 후 의존성을 해제한다. 키·실제 env·외부 write는 접근/실행하지 않았고 최종
  사용자10개 시나리오와 실제 NVIDIA/Skill/NemoClaw/OpenShell gate는 여전히 미완료다.
- OPS-002 첫 명세 갱신은 inactive reservation의 계약 수락만 허용하는 guard에 의해
  거절됐다. 기존 clean/idle feature와 미통합 변경 없음·외부 효과 없음을 확인한 뒤,
  역사적 통합/evidence를 approaches에 보존하며 reservation만 명시 archive-discard했다.
  코드를 삭제하거나 framework guard를 바꾸지 않았고 새 일정 AC를 정당하게 반영했다.

## 팀 Factory 이후 재검증·OPS-002 일정 회귀 통합 — 2026-09-26

- main `85d747e`를 고정한 상태에서 관련 기반을 새 worker/target evidence로 검증했다.
  P0-014는15+50, P0-015는26+4, P0-016은31+82, P0-017은56,
  P0-018은53, P1-006D는22+203, P0-027은 실제 NAT8/미설치2,
  P0-028은 adapter22/실제 EvaluationRun2가 각 환경에서 통과했다.
  과거 결과를 새 source에 복사하지 않았으며 post-teams source/result를 남겼다.
- P0-015의 첫 worker 결과 수집은 실행 도구의 yield를 완료로 오인하여 실패했다.
  해당 failed evidence를 보존하고 worker02에서 종료까지 실제 수집해 다시 검증했다.
  target capture 전에 시작한 검사도 공식 증거로 사용하지 않고 capture 완료 뒤 다시
  실행했다. P0-017의 사전 JS 직렬화 오류1건과 P0-014 report 필드 확인 오류도
  handoff/report에 남겼다. 제품 실패 또는 통과와 운영 수집 오류를 구분한다.
- OPS-002 feature `6a352ab`의 일정 기대값·문서2개만 통합했다(main `1c04a2b`).
  worker94+17 passed, root 독립 current-effort-target-02는
  **94 passed(122.35초)+17 passed(22.51초)**였다. 원래 full-regression-01의
  596 passed/1 failed는 그대로 보존한다. generation8/revision56에서 close했고,
  이 결과는 관리/이관 회귀이며 제품 최종10개 E2E 통과가 아니다.
- P1-001은 같은85 baseline의 dirty 작업을 그대로 보존한 채 coordinator 검토로
  기존 claim만 아카이브했다. 실제로 발견한 tests/test_teams.py의 migration4 기대값을
  owned/V2에 추가하고 generation3/spec9로 재claim했다. reset/stash/파일 삭제는 없고,
  아직 테스트가 실행되기 전이라 이전 실패를 없앤 것도 아니다. P1-006은 별도 feature와
  worker-evaluation generation1로12 Persona/행위/Judge 분리 구현을 시작했다.
- P1-001A의 다음 검색/무효화 작업은 read-only preflight를 진행했다. main의
  authorization.py 및 준비 input 파일명을 잘못 가정한2번의 조회 실패는 실제 파일
  탐색으로 정정했으며 제품 검증으로 집계하지 않았다. 알려진 누락은 metadata 선필터,
  무관 자료 fallback 제거, 완료 replay/GET와 세션 assistant message의 최신 ACL 검사다.
  KB 새 계약 발행 전까지 reference 계획이며 구현/통과를 주장하지 않는다.
- 현재 main 전체 suite를 다시 실행할 예정이며 KB/평가 feature 검증과 별도로 기록한다.
  실제 NVIDIA/Skill/NemoClaw/OpenShell, 최종10개 E2E는 여전히 미실행 gate다.

## 전체 회귀 복구 확인 — 2026-09-26 12:00 UTC

- main `9871f1d`에서 새81개 source hash·67개 task spec digest를 검증 시작 전에
  캡처한 뒤 `.venv/bin/python -m pytest -q`를 실제 실행했다.
  **597 passed,77 warnings(184.68초), skip/fail/error0**이며 이전 일정 기대값 실패가
  수정된 것을 확인했다. `OPS-002/full-regression-02/{source,result}.json`에 보존했다.
- 기존 LangGraph subgraph durability 및 dependency ast.Str 경고는 숨기지 않았다.
  아직 미통합인 KB/Persona feature나 최종10개 E2E, 실제 NVIDIA/Skill/NemoClaw/
  OpenShell·승인/게시 gate를 이 전체 회귀 결과로 대신하지 않는다.

## P1-006 Persona/행위 평가 통합·P1-001 KB 제출 — 2026-09-26

- P1-006 source `72a66d4`의5개 파일을 main `c7f7e4f`에 통합했다. 기존24 ID와 새12사례,
  실제 native 실행/ledger/spy·합성 sink·품질 Judge 상태를 구분한다. `rfa evaluate`는
  Settings/dotenv 전에 분기하고 private 임시 DB만 사용하며 disabled/mock Judge만 허용한다.
- worker01 V1은41 passed/5 failed, V2는89 passed였다. BU fixture에 company가 있다는
  잘못된 기대2건과 macOS 임시 `/var` symlink를 거절한 실제 trace guard 관련3건이었다.
  신뢰된 평가 임시 경로만 resolve하고 guard는 유지했다. 독립 검토로 찾은 incomplete
  retrieval/approval 누락, 실제 위반을 unknown으로 낮추던 우선순위, 변경 전 결과의
  잘못된 재사용도 수정했다. 같은 입력 재전송은 실제 typed 중복 거절과 호출 수로 확인한다.
- worker02 **57+89 passed**; root `evaluation-target-02`도
  **57 passed(13.16초)+89 passed(6.28초)**, fail/error/skip0으로 검증했다.
  generation1/revision26에서 close했다. 실패01과 성공02 및 독립 target evidence를 보존한다.
- 실제 합성 CLI는4.713초·exit1이었다. C03/04/05 pass, C01/02/06은 기존 BU 회사
  확인 누락에 따른 범위 실패, C11은 ACL 변경 뒤 실제 GET 재노출 실패였고 나머지는
  unknown이다. 이를 평가기 테스트 성공과 구분한다. semantic quality와 product final은
  not_run이며 mock 점수로 보안 gate를 상쇄하지 않는다. 실제 자료/외부 게시는 사용하지 않았다.
- P1-001 source `09bdb65`의16개 파일은 worker 제출·독립 read-only 검토를 마쳤다.
  knowledge-01은 필수31+176 pass 후 추가 회귀39 pass/1 fail: SQLite가 정상 unlink한
  열린 sidecar fd의 link0을 거절한 것이 원인이었다. 최초 보충 명령의 잘못된 test 파일명은
  별도 미수집 오류로 보존했다. DB의 link1 검사는 유지하고 sidecar만 owner/regular의
  link0/1을 허용한 knowledge-02는 **37+176+40 passed**였다. 실패 report의 unset
  Settings repr 한 줄은 민감표현 guard가 거절하여 완전히 제거하고 나머지 안전한 원인/횟수를
  기록했다. 현재 KB는 target 통합·계약 발행 대기이며 이 단계의 결과를 전체 E2E로 주장하지 않는다.
- 다음 P1-001A 명세는 기존AC를 유지하며 metadata 선필터, current-head, 과거 result와
  세션 assistant message의 무효화, 실제 KB API를 쓰는 C11 consumer 전환을 구체화했다.
  새 KB 계약·직접 선행 결과 수락 전에는 draft이며 테스트는 planned다.
- 발견한 별도 `.gitignore` 미커밋 변경은 사용자 변경으로 보존했고 우리 커밋에 넣지 않았다.
  아직 생성되지 않은 평가 handoff를 조회한 진단은 missing이었으며 실행 증거로 삼지 않았다.

## P1-001 KB 통합·계약 발행 — 2026-09-26 12:20 UTC

- source `09bdb65`를 main `30ad534`에 통합했다. owner 인증된 자료 CRUD/import,
  불변 revision/current-head, 중복 입력·삭제 tombstone·legacy restricted 처리를 제공한다.
  검색 권한 선필터와 과거 응답 무효화는 별도 P1-001A이며 아직 구현했다고 하지 않는다.
- frozen DTO1.0은 그대로 두고 repository-local EXTENDED1.1 digest를
  `70f3416bd5113bbc1516104314971404ff84d9aa61b569542f5d503164a54a21`로 발행했다.
  main의 schema/OpenAPI·9 fixture 및 extended7 fixture 검사를 실제 실행했다.
  이는 팀원 실제 API 합의나 연동 검증이 아니다.
- 독립 target02는 **37 passed(0.35초)+176 passed(14.02초)+40 passed(6.46초)**,
  fail/error/skip0이었다. 검증 전 source를 캡처했고 generation3/revision22에서 close했다.
  실패 knowledge01과 worker02, target02 및 계약 발행 evidence를 연결해 보존한다.
- P0-017/018/028/P1-006 consumer가 additive 계약을 검토·수락했다. 새 source로
  관련 기반만 재검증하며 기존 구현을 다시 만들지 않는다. 현재 수정 대상은 ACL 기반
  검색과 GET/replay/session message의 안전한 projection, 평가 C11의 실제 KB API 사용이다.
- 실제 NVIDIA/Skill/NemoClaw/OpenShell·승인/게시 및 최종10개 시나리오는 이 결과와
  별개의 미완료 gate다. 사용자 `.gitignore` 변경은 계속 보존하고 커밋에서 제외한다.

## KB·평가 통합 후 전체 회귀 — 2026-09-26 12:28 UTC

- main `76b2186`에서87개 source hash·67개 명세 digest를 실행 전에 캡처했다.
  키/실제 env·생성 상태·증거·사용자 `.gitignore`는 fingerprint에 포함하지 않았다.
  `env -i`의 명시 PATH/control root에서 정상 pytest plugin과 설치 NAT를 사용했다.
- `.venv/bin/python -m pytest -q`: **687 passed,124 warnings(185.54초)**,
  fail/error/skip0. 실행 종료 뒤 HEAD와87개 source 내용이 변하지 않았음을 재확인했다.
  `OPS-002/full-regression-03/{source,result}.json`에 실제 명령·시각·결과를 보존한다.
- 기존 subgraph durability/ast.Str 경고 및 인증 필드를 의도적으로 변조한 음성 fixture의
  Pydantic 경고를 숨기지 않았다. 평가기 테스트 성공은 Persona 제품 사례 전부 통과,
  최종10개 E2E 또는 실제 NVIDIA/Skill/NemoClaw/OpenShell·게시 성공을 의미하지 않는다.

- P1-001A 시작 경계를 현재 source로 확정했다. BU와 별도인 trusted project resolver,
  실제 단기 정책 receipt, 불변 부모 metadata, 기존 fingerprint 호환, outward만 제한하는
  ACL 회수 처리와 C11의 정상 KnowledgeService CAS를 AC/검증에 연결했다. 기존 ID·AC와
  1.0 계약을 유지했고 P1-006 직접 의존성을 추가했다. spec ready와 실제 실행 가능은
  별개이며 선행/예약 해제 뒤 claim한다. 이 명세 검증은 새 제품 기능 검증이 아니다.

- P1-001A source 준비는 `task/P1-001A`/별도 clean worktree와 기존 patched Python을 사용했다.
  offline locked NAT extra 설치 뒤 Python3.12.13/SQLite3.53.1/NAT1.8.0을 실제 확인했다.
  배포명을 `nvidia-nat`로 조회한 준비 오류는 실제 `nvidia-nat-core/langchain/eval`로
  정정했다. V2의 과거 잘못된 `tests/test_baseline.py` 경로도 파일 조사로 발견해 실제
  `tests/test_contract_baseline.py`로 claim 전에 정정했다. 이 준비를 제품 테스트로 집계하지 않는다.

## post-knowledge 기반 재검증 완료 — 2026-09-26 12:43 UTC

- main76b2186 고정, source 수정 없이 새 worker/target evidence를 각각 만들었다.
  P0-014 15+50, P0-015 26(+API4), P0-016 31(+회귀82), P0-017 56,
  P0-018 53(+보조78), P0-019 38+182, P1-006D 22+203가 통과했다.
  P0-027은 실제 설치8/별도 NAT 미설치2, P0-028은 adapter22/실제 EvaluationRun2/
  별도 미설치2가 각각 통과했다. P1-006은57+89로 평가기와 계약 회귀를 확인했다.
- 모든 task를 새 target 증거로 integrate/close했고 claim을 반납했다. 과거 실패·통합
  근거는 approaches/evidence와 Git에 보존했다. 이번 pytest 실패·재시도는 없었다.
  NAT 배포명 조회 준비 오류는 실제 core/langchain 명칭으로 정정하고 handoff에 남겼다.
- 실제 평가 CLI는 여전히 exit1: C01/02/06 fail, C03/04/05 pass,
  C07/08/09/10/12 unknown, C11 error다. 불변 KB가 기존 변경을 거절했기 때문에
  C11 past_result는 unknown/변형not_run이며 과거 결과를 새 누출 증거로 쓰지 않았다.
  P1-001A가 합법적인 KB 변경과 현재 조회의 권한 검사를 연결할 차례다.

## 사용자 추가 승인 — 교체 가능한 최소 로컬 모듈

- 사용자가 승희/다영 모듈의 로컬 구현이 없다고 확인하고, 우선 아주 기본으로 만들되
  나중에 교체 가능하게 모듈화하도록 승인했다. 기존 포트/reference 계약을 재사용하여
  로컬 승인·게시 상태/MCP 경계, runtime 경계와 최소 UI를 준비한다.
- 팀원의 최종 서비스 책임이나 기밀 검수 소유권을 임의 변경하지 않는다. stand-in의
  DB/승인 상태와 core mirror를 구분한다. 실제 외부 게시·배포 권한으로 확대하지 않고,
  기본 runtime을 OpenShell 격리로 주장하지 않는다. 실제 모듈/OS 강제 gate는 별도로 유지한다.
- 해당 기존 task의 scope·분할/실제 gate를 확인하여 반영할 예정이며, 현재 검색 개발을
  중단하지 않는다. 새 framework나 중복 MSA가 아닌 기존 adapter 교체 지점을 사용한다.

## P1-002A NVIDIA hosted live 증거 — 2026-09-26 14:30 UTC

- 사용자가 NVIDIA 호출 증거를 먼저 진행하도록 우선순위를 바꿨다. P1-002A spec r2로 P1-002 의존을
  제거하고 고정 합성 한국어 요청만 보내는 direct hosted smoke로 범위를 한정했다. 제품 코드와 공유
  파일은 변경하지 않았고 제품 ModelPort 경로는 not_run(P1-002)이다.
- hosted `nvidia/nemotron-3.5-lightning-30b-a3b`에서 한국어 근거 답변+[E1] 인용, 근거 밖 '근거 부족',
  json_object Pydantic 검증, named tool_choice 제안(실행 0회)을 확인했다. worker v1-live-a1 4 passed
  38.7s, target(main 2c880f2) v1-live-target-a1 4 passed 117.2s, 재시도 0.
- 오류와 수정: 첫 순차 개발 실행은 hosted 지연으로 2건 ReadTimeout(150s). 4요청 동시 전송과
  timeout/429/5xx 1회 재시도, 실패 시도 기록으로 바꾼 뒤 통과했다. 지연은 1~117초로 편차가 크다.
- 키는 Settings(_env_file) SecretStr 안에서만 사용했고 로그·evidence·control 파일에 없음을 값 출력 없이
  검사했다. integrate/close 완료. 다음: P1-002 제품 adapter가 이 관측(지연 편차, reasoning token 비용,
  thinking off+json_object)을 반영한다.

## P1-001A 완료·계약 발행·재검증 cascade — 2026-09-26 14:00~14:45 UTC (coordinator: claude-opus-5-5)

- P1-001A 구현(권한 선필터 local lexical 검색, current-source outward projection, C11 KB 회수)을 이어받아
  첫 전체 실행 34 failed/683 passed의 원인을 로그로 분류해 수정했다(1회 cycle): 테스트 fixture 형식 오류,
  mock 전용 시나리오의 local 기본값 실행, raw SQL 본문 변조의 정상 fail-closed 탐지, outward policy 재판정
  관측으로 깨진 trace 테스트 가정, extended 계약 재생성. 제품 606 passed, 평가 CLI C01–C05 pass/C06–C12
  unknown(미구현 gate)/C11 acl·past_result pass, exit 2.
- spec16에서 scope에 scripts/contract_baseline.py·tests/test_trace_contract.py·tests/test_nat_smoke.py를
  추가(AC/검증 축소 없음) → worker/target V1 84·V2 348 passed → commit 66b2d49 통합, RFA-EXTENDED 1.1
  f711bab8 발행, close(62422a2).
- 발행/공유 소스 변경으로 done 11개가 verifying/stale이 되었다. `.agent/input/tc.py`(taskctl 래퍼: 최신
  revision/generation 자동, 검토된 pytest 계획만 env -i로 실행해 report 작성)와 revalidate 루프로
  P0-014/015/016/017/018/019/027/P1-006D를 새 worker/target evidence로 재검증·close했다.
  OPS-002 첫 재검증은 격리 env에 TASK_CONTROL_ROOT가 없어 task_migration 9건이 실패(제품 결함 아님) →
  env 보완 후 재시도 중.
- 같은 저장소에서 사용자가 연 다른 세션이 NVIDIA 실제 증거(P1-002A 완료, P1-003A 진행)를 담당하고 있음을
  commit/claim으로 확인했다. 충돌을 피하기 위해 이 세션은 P0 기능·재검증·E2E를 맡고 NVIDIA 실제 연동
  task(P1-002/002A/003/003A/006A)는 건드리지 않는다. 다른 세션이 claim 1개를 쓰므로 project max_workers를
  규칙상 상한인 3으로 올렸다.
- 사용자 승인(teammate stand-in)에 따라 P1-008C/P1-008D/P0-025A를 등록했다(docs/LOCAL_MODULES.md).
  P1-001B·P1-006E와 P1-008C→D→P0-025A는 새 파일만 소유하므로 보조 worker가 wip branch에서 구현하고
  coordinator가 claim/evidence/통합을 맡는다.
- P0-020(역할 실행·Supervisor 수집·팀 예산·취소)을 구현 중: workers.py(TeamRunner, 역할 handler, 공유 예산,
  SupervisorBus), migration6(run_team_bindings/role_executions/team_run_results), LocalRuntime 역할 key별
  status/cancel, allowlist 로컬 READ 분석 도구, supervisor graph 팀 분기, owner-target domain 검색의
  audience cap 교집합 버그 수정. 신규 test_team_execution 9개와 제품 615개 통과(claim 전 개발 확인).
  P1-001 예약 해제(재검증) 후 claim → 공식 evidence 예정.
- P1-001 재검증 close 후 P0-020을 claim(gen1)했다. commit 86d770f, worker/target V1 68·V2 125 passed,
  RFA-EXTENDED 1.1 888c3d6d 발행, integrate/close. 오류 수정 이력: 첫 개발 실행에서 EvidenceItem.title
  부재, Supervisor 자기 전달 거부, research 출력 이름, 동시 ensure provisioning 대기, owner target private
  audience 교집합 누락을 로그 기반으로 수정했다(claim 전, 제품 테스트로 확인).
- OPS-002 재검증: 두 번째 시도는 동시 실행 부하로 test_task_migration이 120초 timeout(exit -9). 제품 결함이
  아닌 환경 부하이며 cycle 한도(3) 안에서 단독 실행으로 재시도한다.

## P1-003A NVIDIA 공식 Skill(NeMo Retriever) live 증거 — 2026-09-26 15:30 UTC

- 사용자가 공식 Skill 증거를 다음 우선순위로 지정했다. P1-003A spec r2로 P1-003 의존을 제거하고
  build.nvidia.com Skills 카탈로그의 nemo-retriever Skill(26.8.1) direct smoke로 한정했다. 제품 코드와
  pyproject/uv.lock은 바꾸지 않았고 retriever는 저장소 밖 pinned tool venv에 설치했다.
- 합성 한국어 PDF 2개(5쪽)를 retriever ingest(--method pdfium)/query --format evidence로 실제 실행했다.
  CPU 호스트라 hosted embedding(llama-nemotron-embed-vl-1b-v2)을 사용했고 키 없는 query는 실패했다.
  3질문 top-1 source/page가 정답이었고, 검색 근거만 넣은 Nemotron 답변의 사실·인용을 검증했다.
- worker v1-live-a1 7 passed 51.7s, target(main caf7bd2) v1-live-target-a1 7 passed 58.5s. 개발 실행에서
  Research 답변 1회 ReadTimeout 후 재시도 성공. integrate/close 완료.
- 제품 Research worker→EvidenceBundle(audience/policy/source_revision) 경로는 not_run이며 P1-003 범위다.


## Coordinator 세션 이어받기 — 2026-09-27 00:45~01:45 KST (15:45~16:45 UTC)

- post-team 재검증 cascade 완료: P0-028, P1-001A, P1-006, P1-006D worker/target 재검증 후 close(2aba236 커밋).
- **OPS-004 등록·완료**(7fa9823 → merge 7553a20): cProfile로 `taskctl ready` 22.2초 중 약 20초가 done task마다
  같은 target에 git을 반복(1,551회)한 것임을 확인. `cli.main()` 호출 단위 git memo 추가. test_taskctl 100 passed,
  같은 control 상태에서 ready 실측 2.0초(이전 22~26초). 규칙·fingerprint 비교 변경 없음.
- **P1-008C 통합·완료**(8de4cc6 → merge a0a9347, worker/target 24 passed). stand-in 검토/모의 게시/READ tool 모듈.
- 오류와 조치:
  1. `nohup … &`로 띄운 재검증·통합 프로세스가 도구 셸 종료 시 함께 종료(로그 없음). 이후 유지 세션에서 실행.
  2. OPS-002 재검증 중 P1-008C 통합으로 target 이동 → submit 거절, claim 중이라 revalidation recover도 불가.
     소스 변경 없는 재검증이라 discard. 이 때문에 OPS-003 stale 예약과 OPS-005 claim이 순환 차단 → OPS-003도 discard.
     두 task는 이후 실제 문서/회귀 변경으로 다시 완료한다(이력은 attempts.approaches 보존).
  3. OPS-005 worker 단계 V2(view 정합성)는 규칙 변경이 파생 view를 바꾸기 때문에 기존 도구로는 통과 불가 →
     worker 단계 evidence 명령만 후보 taskctl로 실행(`.agent/input/verify_with_candidate_tool.py`), submit·target·close는
     정식 도구. target 103+17 passed.
  4. OPS-005가 global context 문서(TASK_EXECUTION_RULES.md)를 바꿔 모든 통합 task가 stale(context digest). OPS-005
     규칙대로 다음 claim의 직접(전이) 의존성만 재검증: P0-014, P0-015, P1-008C 완료, P1-001A는 P1-001 선행 필요 → 재시도.
- **OPS-005 등록·완료**(7228c6e → merge 4bf0ec9): 활성 claim·미통합 제출 없는 stale 통합 예약은 새 claim을 막지
  않음(의존성 gate·최종 전체 재검증 유지), 재검증 중 target 이동은 clean FF만 무변경 제출 허용. 새 테스트 3개, 각 보정을
  되돌리면 실패함을 확인.
- **P1-005A 개발**(wip/P1-005A 26f172c, 미통합): DraftLifecycle(불변 버전·승인 유효성·durable PENDING 영수증·
  outcome_unknown은 조회로만 대사), MockPublisher, 게시 상태 전이표, migration 8, `/v1/runs/{id}/draft*`·
  `/publication` API. tests/test_draft_lifecycle.py 15 + test_state_machine.py 76 passed.
- E2E harness(P0-026 wip a4a57c6) 보고: 39 passed, 기능/보안 gate 통과 가능 범위 확인. 품질 gate 실패(Recall@5 0.52~0.61,
  목표 0.9), 후속 비교 비결정성, team/cancel HTTP 경로 부재, identity provisioning 부재 → 보수 task(P1-001D, P0-020A,
  P0-020B) 개발을 보조 worker에 배정.
- 병렬 개발 배정(코드만, claim/evidence는 coordinator): ledger_worker(P0-021→P1-008), scheduler_worker(P0-022→P0-024,
  APScheduler 3.11.3), quality_worker(P1-005B, P1-006B, P1-001D, P0-020A, P0-020B). migration 번호 9~12 예약.
- 다른 세션이 wip/P1-003(nemo-retriever Skill CLI를 Research ToolPort로 연결, 914322c/6b79237)을 coordinator 검토용으로
  남겼다. P1-005 통합 뒤 claim/통합 대상에 포함한다. 병렬 분담 질문에 대한 사용자 답은 아직 없고 기존 가정(NVIDIA 실제
  연동은 다른 세션)을 유지한다.


## Coordinator — 2026-09-27 01:45~02:25 KST (16:45~17:25 UTC)

- 통합·close: P1-008D(ab9cf68), P0-025A(9336690), P1-001B(559c366), P1-004A(0bdef6a, RFA-EXTENDED 003af117), P1-004B(d3cad28, 25928d88), P2-003(0161cc2, 9455b302), P1-005(d8c596d), P1-005A(9178e20, b2e601b8).
- 각 발행 뒤 다음 claim의 직접(전이) 의존성만 재검증(OPS-005 규칙). `revalidate_par.py`에 REVAL_ONLY + stale 전이 폐포를 추가했다. 처음 버전은 이미 done인 의존성의 하위까지 다시 열어 불필요한 재검증이 생겼고, stale 집합으로 제한하도록 수정했다.
- 오류와 조치:
  1. P1-001B claim 거절(spec unresolved) → digest 고정 edit-spec 후 성공.
  2. chain 작업(P1-004A~P1-005A)의 wip commit이 owned_paths 밖 파일을 건드림 → coordinator edit-spec으로 scope 추가(bootstrap, contracts/__init__, extended.json, CHANGELOG, migration 단언 테스트).
  3. migration 7 추가로 고정 목록 단언 3건 실패 → 0cee5e4에서 "빈틈없는 1..N" 단언으로 교체.
  4. P1-006E 레드팀: P1-005가 근거 로딩을 staged-context로 옮겨 retrieval test double이 호출되지 않음(5 failed) → 공격을 합성 공개 KB note로 심고 서버측 읽기·screen 보류를 관측하도록 보수(b9bb0de). RT03 strict xfail은 실제 단언으로 전환. screen을 끄면 2건 실패함을 확인.
  5. test_behavior_verifier 4건 실패(P1-005 이후 retrieval spy 0회) → P1-005C 등록, staged-context 경계를 container에 노출하고 spy가 관측(93fe378, 57+1 passed).
- 보조 worker 결과: P0-021(effect ledger, migration 9, 47 passed), P1-008(reference 계약 suite 60 passed, HTTP publisher·callback verifier), P0-022/23/24(APScheduler 3.11.3 + SQLAlchemy 2.0.54 pin, migration 10/11, test_schedules 43 passed), P1-005B(피드백 4분류, migration 12, 16 passed), P1-006B(persona 24사례, mock 기준 security 4 fail은 P1-005 screen 이전 main 기준), P1-001D(Recall@5 small20 0.61→1.0, load1000 0.52→1.0, 같은 harness·seed), P0-020A(결정적 후속 비교), P0-020B(team/cancel HTTP). 모두 wip branch이며 ledger_worker가 선형 통합 stack(wip/stack)으로 충돌을 해소 중이다.
- 설정 점검: main checkout에는 `.env`가 없고 NVIDIA 키는 명시 env 파일(`.env.dev`, 값 미열람)로만 live 테스트가 사용한다. doctor 기준 LANGFUSE_* 미설정 → P1-006C live는 로컬 self-host(colima) 시도 또는 not_run.


## Coordinator — 2026-09-27 02:25~04:05 KST (17:25~19:05 UTC)

- 통합·close: P1-006 재통합(5558842), P1-004(6f01052), P1-006E(fc82677/204d2b6), OPS-002 재close(18b3ea0), P0-021(3c5cb6f), P1-008(3466bc5), P0-022(8c99475), P0-023(bbd80e3). 매 발행 뒤 다음 claim의 stale 의존성 폐포만 재검증했다.
- 오류와 조치:
  1. P1-005 통합 뒤 P1-006 재검증 4 failed(retrieval spy 0회). P1-005C가 P1-005→P1-001A→P1-006 순환 의존으로 claim 불가.
     - P1-006 재검증 claim을 discard하고 scope를 넓혀(bootstrap, 새 경계 테스트) 실제 변경으로 재통합했다(b58fb11).
     - P1-005C는 claim된 적 없는 상태에서 superseded로 취소했다(관리 스크립트, 사유 기록).
  2. P0-022 pin(APScheduler 3.11.3, SQLAlchemy 2.0.54) 병합 후 재검증 worktree venv가 옛 lock이라 pytest가 수집 0으로 실패(P0-014).
     - `tc.finish`/FF 재시도에 `uv sync --locked`를 추가하고, intcoord는 병합 후 canonical venv도 동기화한다.
     - 재시도 1회로 진행.
  3. P1-002A: 다른 세션 claim(만료)의 worktree가 baseline 이후로 FF되어 submit 불가. 복구는 P1-003A stale 예약과 EDUCATION_MAPPING 겹침으로 거절.
     - chain 종료 뒤 P1-003A live 재검증 → P1-002A 인수 순서로 처리 예정.
     - live 계획의 `env RFA_*=…` 형식을 tc.run_plan이 받도록 했다(RFA_* 변수만, argv는 계획 그대로 기록).
- 보조 worker 결과:
  - P1-007: NemoClaw 공식 지원 agent는 3종이라 LangGraph 앱은 sandbox 밖 API로 설계. NemoClaw 실행은 not_run.
  - P1-007C(등록): 실제 OpenShell v0.1.1 VM driver로 E2E-05 허용/차단 매트릭스 14행 기대 일치. n=3 sandbox/역할, run 0926181417.
  - P1-006C: OTLP export, 로컬 Langfuse 4.46.0 live에서 ID 조회·삭제 확인. OSS가 보존 설정을 무시함을 확인 → P1-006F blocked 분리.
  - P1-002: 제품 경로 NVIDIA 실제 호출 n=1, 29.8s. P1-003: 제품 Research→공식 Skill CLI 실제 n=1. 다른 세션 wip을 rebase·통합 준비.
  - P0-026 harness v2: 64 passed/1 skip(real-model opt-in).
- E2E harness가 찾은 결함과 배정:
  - SQLite 두 번째 프로세스 SIGBUS(높음) → P0-005A.
  - 추출 품질 → P1-004C.
  - 근거 부족 답변 → P1-001E.
  - 승인 전 게시의 영구 실패 receipt → P1-008E.

## Coordinator — 2026-09-27 작업 완료 중심 재개

- 사용자가 다른 세션 종료와 단일 책임자의 완료 중심 진행을 요청했다. 남아 있던 `freeze-reval3` 자동 재검증 프로세스와 그 테스트 자식을 중단했다. 기존 커밋·실행 결과·사용자 `.gitignore` 변경은 보존한다.
- 반복 원인: 공유 계약 발행과 `docs/INTEGRATION.md` 같은 넓은 입력 경로 변경이 통합 완료 task를 stale로 만들었고, 각 통합 앞에서 전이 의존성의 worker/target 검증을 반복했다. 예: P0-020/P1-006C는 마지막 성공 후 INTEGRATION 문서만 달랐으며, P0-005A는 실제 local/settings 소스 변경도 있었다. 두 경우를 구분해 검토한다.
- 실행 순서를 고정한다: 남은 선행 검증 → 기존 WIP(Judge·보존 처리·OpenShell/NemoClaw 증거) 회수 → P0-026 E2E 통합 → P1-009 전달 문서 → 최종 변경 범위 검증. 실패는 로그를 보고 최대 3회 수정하며 전체 자동 재실행을 다시 걸지 않는다.
- 이미 main에 반영된 P0-024~025·피드백·검색/추출 품질·SQLite 다중 프로세스 보수, NVIDIA 모델·Skill 경로의 커밋과 evidence를 보존한다. P1-002A의 JSON 응답 truncation 실패 뒤 1회 재시도 성공도 실패 기록과 함께 남아 있다.
- 새로 회수한 미통합 결과: P1-006A Judge(actual, 합성 public n=1), P1-006F 자체 보존 삭제 job(local Langfuse), P1-007A NemoClaw 제한 API 시연(n=1). 구현/실행 기록이 있다는 사실과 main 통합·최종 사용자 흐름 통과를 구분한다.

## Coordinator — 완료 중심 통합 결과 (2026-09-27 10:10 KST)

- OPS-007 `4e0a55b`: 기존 통과 증거와 정확한 문서-only delta를 검토하는 명령을 추가했다. 13개 테스트를 worker/target에서 통과했다. 원래 실행 시각을 보존하고 `tests_reexecuted=false`로 기록한다. 코드·설정·계약·AC 변경에는 사용하지 않는다.
- P1-007C `fa2a882`: standalone OpenShell 실행 기록과 재현 스크립트 통합, offline 4개 + recorded-live matrix 14행 검토. 팀원 Runtime identity 증거로 대체하지 않는다.
- P1-006A `096ff8c`: NVIDIA Judge adapter/opt-in 배선·기록된 실호출 JSON 통합. offline 138개와 recorded-live 합성 public n=1을 구분했다. Judge 0.6은 보안 통과나 사용자 만족도 수치가 아니다.
- P1-007A `d7d5bbb`: NemoClaw 제한 API 시연 기록 통합. 호스트 RFA backend는 sandbox 밖이다. 5개 허용 요청/7개 거절 probe와 CLI replay 오류/모델의 403 설명 오류도 기록했다.
- P1-006F `4542713`: community Langfuse 자체 보존 job 통합 완료. 중복 `LANGFUSE_RETENTION_DAYS` 대신 기존 `TRACE_RETENTION_DAYS` 재사용, proxy env 비활성, 명시적 service 표식 요구, 타 앱 span 혼합 trace 제외, 스캔 예산 초과 시 삭제 0건으로 보강했다. worker/target 각각 111 passed. 기존 branch `8f80676`의 live 6 passed/전용 MinIO 실험은 과거 실행 기록으로 유지하며 이번에는 운영 데이터 삭제를 실행하지 않았다.
- P1-001E `0ab54f1`, 통합 `54118a0`: Persona `OMEGA` 질의가 일반 단어만으로 근거를 얻던 실제 결함 수정. ALL-CAPS 명시 식별자가 허용 자료에 없으면 insufficient. 기존 소문자 동의어·접속사 규칙 유지. 단위/검색/context 65 passed, pinned E2E harness 4 passed(20문서 3회+1000문서; Recall@5 각각 1.0). target에서도 동일 4+65 통과. Persona v2는 수정 전 23/24→수정 후 24/24, 보안 실패 0; mock/simulated이며 semantic quality는 not_run.
- Recall 측정 harness commit은 `ea76550`, 제품 source는 worker `0ab54f1`/target `54118a0`이다. 외부 harness의 `code_commit` 필드를 제품 commit으로 오인하지 않도록 evidence에 분리 기록했다. 최종 P0-026 통합 후 자체 harness 결과로 다시 묶는다.
- 오류 처리: P1-001E 명세 갱신이 inactive reservation 때문에 거절되어 reservation만 명시적으로 해제 후 재시도했다(코드/이력 삭제 없음). P1-006C 재검증은 manual 관찰 인자 누락으로 실행 전 정지했고 실제 기록 검토를 전달해 완료했다. 보존 WIP의 INTEGRATION/WORK_LOG 충돌은 최신 문서를 유지하고 작업 로그를 이 절로 합쳤다. 무조건 재시도 루프는 사용하지 않았다.
- 공식 제출 폼 재확인: 웹 도구 접근 실패, 직접 공개 HTTP 조회도 401. 브라우저 surface가 제공되지 않아 로그인 UI 우회하지 않았다. 기존 사용자 제공 요건은 유지하되 이번에 최신 내용을 확인했다고 표시하지 않는다.
- 다음: 코드 변경을 더 늘리지 않고 P0-026에 필요한 선행 증거 → 10개 controlled 시나리오 → 전달 문서/잔여 real·UI gate 정리. 미검증 actual gate를 mock 결과로 완료 처리하지 않는다.

## Coordinator — 최종 인수 연결 (2026-09-27 10:25 KST)

- P0-019 `93ae0a3`: 새 relevance 규칙이 차단한 unknown 대문자 질의에서 source-policy 호출을 기대하던 trace 회귀 fixture를 보수했다. 허용 근거가 있는 기존 경로와 근거 없는 새 경로를 각각 단언한다. case-insensitive canary 검사도 유지하며 worker/target 각각 38+183 passed. 제품 권한/검색 규칙을 완화하지 않았다.
- P0-026의 README/INTEGRATION/EDUCATION 소유권 중복을 기존 P1-009로 정리했다. P0-026은 demo/fixture/harness/DEMO.md를 소유하고 제품 소스 전체를 read-only fingerprint로 검증한다. AC와 실제 gate는 그대로다. 최종 보고 문서만 갱신해 E2E 전체가 stale이 되는 순환을 줄인다.
- revalidation 예약 충돌은 실패 테스트로 간주하지 않고 시작 전 owned scope로 직렬 배치한다. 진행 중 작업이 없어도 같은 claim을 반복하던 경로는 중단하도록 보수했다. 완료한 선행은 재실행하지 않으며 남은 final dependency만 처리한다.
- 실제 Chrome 154 / Playwright 1.63으로 합성 로컬 UI를 확인했다: 세션 생성, 노트 저장, 공격처럼 생긴 제목의 text 렌더링, waiting_approval → 수동 mock 승인 → completed. 외부 outbound/JS 오류/브라우저 저장소 기록 없음. 첫 시도는 macOS 임시 경로 symlink의 안전 경로 검사로 503; 경로를 resolve한 두 번째 시도가 통과했다. 정책 검사를 끄지 않았다. 전체 10개 UI E2E나 실제 게시 증거는 아니다. 증거: `/var/folders/4l/brc8y7px63929n3z3lhxfms80000gn/T/rfa-ui-completion-proof-p534yzmp/result.json`.
- 공식 제출 폼의 공개 브라우저 재확인도 로그인 페이지로 이동했다(2026-09-27 01:23:39 UTC, 새 임시 프로필, 입력/제출 없음). 기존 사용자 제공 제출 요건을 유지하고 최신 폼 내용 확인은 불가로 남긴다.
- NVIDIA 실제 대표 3개 시나리오×3회 측정 진행: 처음 두 시나리오에서 HTTP 연결은 되었으나 길이 제한 실패와 30초 성능 목표 미달을 발견했다. 성공 숫자만 요약하지 않고 실패/지연/기밀 marker 검사를 분리 보존한다. 추가 비교는 최대 3개 설정 내에서 오류 근거가 있을 때만 수행한다.
- 실제 모델 비교 종료(총 3개 설정, 설정별 E2E-02/06/08 각각 3회): Lightning/1024는 7/9 완료, Lightning/4096은 8/9 완료이며 둘 다 `model_truncated`와 30초 지연 목표 미달이 남았다. 마지막 Ultra(`nvidia/nemotron-3-ultra-550b-a55b`)/4096은 9/9 완료, API 왕복 1,077.844~4,780.827ms, 모든 시나리오 기능·보안/실제 모델/30초 성능 gate 통과. E2E-02의 핵심 사실 기준도 통과했지만 E2E-06/08 의미 품질은 not_run이다. 전 설정 private outbound marker 0. NVIDIA 외의 검색·정책/runtime은 local, 검토·게시는 mock이다.
- 위 비교는 `.env.dev`를 편집하지 않고 명시적 test 설정 override로 수행했다. product source `93ae0a3`, pinned harness `ea76550`; raw report의 code_commit은 harness를 가리키므로 제품 버전과 분리 기록한다. 증거 디렉터리: `/tmp/rfa-completion-real-model`, `/tmp/rfa-completion-real-model-4096`, `/tmp/rfa-completion-real-model-ultra`. 성공할 때까지 반복하지 않고 세 번째 설정에서 종료했다. 기본 모델을 몰래 바꾸지 않으며 전달 문서에 검증한 Ultra 실행 선택을 안내한다.

## Coordinator — 최종 인수 완료와 전달 (2026-09-27 11:00 KST 이후)

- P0-026 기존 WIP `ea76550`을 `7aadfa3`으로 회수하고 CLI retention import를 보존했다. 수정 `82b6919`, `72904f5`, `d6aaa91`까지 main 통합. 제품 graph/권한/예산 제한을 바꾸지 않았다.
- 첫 두 E2E worker cycle: 각각 demo11 pass, scenario54 pass/3 fail. 실제2회 tool 호출인데 한도3에서 초과를 기대한 fixture, 이어서 최소한도3인 Benchmark가 한도1에서 생성 전에 거절하는 정상 동작을 잘못 기대한 fixture였다. `d6aaa91`은 정상·생성 전 거절·검색을 반복하는 역할의 실행 중 초과를 분리했다. 동일 실패를 무작정 반복하지 않았고 실패 source/result를 보존했다.
- 세 번째 worker `completion-worker-0927015257`: 11+57+6+1=75 passed, 실패/skip0. main `completion-integration-0927015649`에서도75 passed. 실제 subprocess SIGKILL/재기동·동시 SQLite reader·예약복구·n100 부하 포함. scenario/variant65개와 미실행 gate를 수동 대조하고 P0-026을 done으로 닫았다. 이후 제품 전체 E2E를 다시 실행하지 않는다.
- P1-009 `6e520f9`/`6cd4597` 기존 문서를 회수하고 `191378e`로 현재 README/INTEGRATION/교육·실패 포함 E2E 보고·안전한 측정 JSON·Chrome 증거·모델 비교 재현 helper를 정리했다. main fast-forward 완료. 공통 계약/설정/제품 소스 변경은 없고 frozen INTEGRATION 문서만 명시적 사유로 허용했다.
- 문서 검증: 로컬 Markdown 링크 누락0, JSON 파싱65 controlled+9 real-model report, helper --help, diff check, ruff check/format. helper lint 첫 실패 B023은 closure의 original 기본 인자 binding으로 보수 후 통과. 문서 V2 demo11 passed. helper 휴대 경로판 추가 후 live 호출을 새로 실행한 것은 아니다.
- P1-009 AC4 공식 폼 본문은 로그인 때문에 미확인이다. 이 조건을 삭제하지 않고 blocked로 남겼다. 이미 main에 반영한 문서의 광범위 reservation만 검사 후 해제하고, 남은 README/e2e-final의 공식 요건 갱신 범위만 예약했다. AC1~3 증거·기존 모든 AC는 유지했다.
- 보고서만 달라진 task는 OPS-007 문서 검토로 원래 실행시각/결과를 유지(`tests_reexecuted=false`)한다. 실제 제품 source/계약 차이는 이 경로로 통과시키지 않는다. UI·relevance·CLI/adapter 등 실제 영향 범위만 현재 고정 main에서 마지막 검증하며 전체 cascade는 다시 시작하지 않는다.
- 사용자의 `.gitignore`8줄 변경은 그대로 보존하며 이번 commit에 포함하지 않는다. 원격 push·게시·배포·실제 외부 write·추가 live 삭제는 수행하지 않았다.

## Coordinator — 고정 소스 검증 종료 (2026-09-27 11:24 KST)

- 최종 제품 baseline `72632e7`을 변경하지 않고 실제 영향 범위 10개 task만 검증했다. P0-017(55), P0-023(56), P1-002(45), P1-003(27), P1-006(68+104), P1-006A(138), P1-006B(13), P1-006C(92), P1-006F(111), P1-007C(4)가 각각 worker/통합 단계에서 통과했고 모두 done으로 닫혔다. 이 실행의 failed/blocked task는 0이다. 숫자는 task별 검사 수이며 중복 없는 전체 제품 테스트 수라는 뜻이 아니다.
- Judge·Langfuse·OpenShell의 실제 실행 항목은 기존 불변 증거를 검토했다. 이번 마지막 검증에서 NVIDIA API 재호출·Langfuse 데이터 삭제·sandbox 재실행은 하지 않았다. 이미 종료한 P0-026 E2E 75개도 재실행하지 않았다.
- OPS-000은 등록된 canonical baseline·비밀 파일 비추적·사용자 변경 보존을 감사하고 임시 control fixture의 잘못된 root/오래된 baseline 거절 2개를 통과했다. 제품 보안이나 실제 서비스 연결 검증으로 집계하지 않는다.
- 최종 원본 집계: done 65 / verifying 5 / todo 1 / blocked 4 / deferred 9 / cancelled 1, 총85개. 실행 중 task는 없다. OPS-001~006은 기존 구현이 있으나 현재 baseline의 관리 도구 감사가 덜 끝난 6개로 유지한다(verifying5/todo1). 이를 제품 미구현이나 완료로 바꾸지 않는다. 관리 체계 재개발·전체 회귀 cascade는 시작하지 않았다.
- blocked: P1-007B의 RFA 역할별 실제 OpenShell identity, P1-008A/B의 실제 팀원 승인·게시/runtime·UI 교체, P1-009의 로그인 필요 공식 폼 본문 확인. 기본 교체형 로컬 모듈은 제공되지만 이 실제 gate를 대신하지 않는다. 기존 deferred9/cancelled1 이력은 유지한다.
- 마지막 확인은 task 원본/파생 view 정합성과 diff 검사에 한정한다. 이후 commit은 task 상태·handoff·이 로그·안전한 불변 evidence JSON만 보존하며 제품 소스나 실행 조건을 바꾸지 않는다. 사용자 `.gitignore` 변경은 여전히 별도 보존한다.
