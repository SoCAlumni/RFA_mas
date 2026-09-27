# P1-008H — 자동 담당 라우팅 / 실제 단계 스트림

## 구현 사실
- 936b4fb / 4b3797e: 저장/질의 의도와 담당 선택 분리. owner active/ready Task의 목표 주제에 맞으면 기존 Task/Team 재사용, 동점/무관하면 비서 검색. TRIV3/양자화는 domain worker이며 Task와 구분.
- 비서 fallback은 기존 authorized_metadata/read_sources로 두 domain의 current ACL 근거만 조회. Run/승인 ID 생성 없음. 저장 공간의 기본 TRIV3는 담당 배정이 아님.
- chat stage/route/refs SQLite 보존; NDJSON 서버 단계와 최종 result; UI chunk reader. 중단은 unknown, 자동 재전송 없음. 실제 worker 내부/토큰 스트리밍 아님.
- main의 공유 파일 동결 규칙 확인 후 core ports/local.py 변경을 feature branch에서 되돌리고 PoC 소유 TeamCatalogPort/SqliteTeamCatalog(read-only)로 분리. core repository get_team_lifecycle에서 소유권 재검증. main의 frozen 파일에는 순변경 없음.

## 검증 및 오류 기록
- 개발1: 기존30개 중28passed/2failed. 일반 질의가 더 이상 임의 TRIV3 run을 만들지 않으므로 기존 run-not-null assertion을 새 직접검색 evidence/현재ACL assertion으로 변경. 보안 AC 축소 아님.
- 개발2: 36passed/1failed. 수동 ASGI streaming fixture의 headers에 대문자가 있어 FastAPI 헤더 조회 실패. ASGI 규격처럼 lower-case headers를 보내도록 수정.
- 개발3: 37passed/0failed/skip. Task/team 재사용, no-match/tie, owner/paused, early frame before work completion, duplicate, disconnect, error redaction, public privacy, 기존 UI/PoC.
- Chrome154 첫 실행: UI 4요청/단계 모두 동작; 소비된 streaming response를 CDP response.text로 재조회할 때 evicted resource 오류. 실행 후 화면/NDJSON header 확인으로 harness 수정. 두 번째 실행 통과: 자동기본, 저장/질의, domain, 무관질의, history, mobile390, JS/outbound0.
- 초기 task 등록 시 미등록 shared-resource 이름으로 refresh1회 거절. 프로젝트의 정확한 resource 이름으로 바로잡음.
- ruff 첫 진단의 import/format/line-length 보수. 후속 shared 파일 동결 대응 catalog 분리 뒤 최종 검증 예정. 과거37pass를 새 adapter 검증으로 대체 주장하지 않음.

## 재개/결정
공유 scope 축소(spec_revision2) 후 canonical discard는 recover-resume 대상이 아니라는 CLI 검사로1회 거절. 기존 feature artifact/커밋을 보존하고 최신 main1906494의 새 worktree `P1-008H-final`에 유효 claim을 받아 순변경만 squash(a771cb5)했다. 동결 core 변경0. 별도 세션의 P1-010/011 통합을 보존했다.

worker01:36pass/1fail(인증거절 fixture에 필수 user_id 누락), Chrome 초기 새 대화와 초기 history 요청의 경쟁으로 화면이 비는 현상. 올바른 unauthenticated fixture를 구성하고 초기 로딩/새 대화 중 전송을 잠그는 1f1844e로 수정. worker02:37passed/실패·skip0, Chrome154 자동저장/검색/no-match/domain/4단계/재로딩/mobile390/JS·outbound0 통과. `.agent/evidence/P1-008H/routing-worker-02/` 참조. 실패01도 보존.

정확한 다음 행동: 검증된1f1844e를 main에 fast-forward 통합하고 target에 같은 scoped 검증 후 close. 기존 사용자8780/18개KB 보존, 서버 교체 후 읽기 확인. 임시18780 서버는 최종검증 후 종료. 전체과거검증 cascade는 시작하지 않는다.
