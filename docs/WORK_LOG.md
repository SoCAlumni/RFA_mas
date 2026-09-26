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
