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
