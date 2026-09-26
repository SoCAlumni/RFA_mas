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

## P0-018 통합 / P0-016 범위 인계 — 2026-09-26T07:50Z

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

## 평가/trace 후속 조사 — 2026-09-26T07:59Z

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
