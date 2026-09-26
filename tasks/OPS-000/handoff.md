# OPS-000 handoff

목표: 사용자 변경을 보존한 Git/worktree baseline 등록
현재 사실(2026-09-26 재조사): Git 초기화는 됐지만 `git rev-parse --verify HEAD` 실패. 모든 프로젝트 파일이 untracked이며 integration_target은 unregistered. 과거 metadata 없음 기록은 당시 사실로 보존한다.
범위: 개발 운영 체계만. 제품 Task/Run ID와 개발 session/attempt를 구분.
검증: git status --short/--show-toplevel/--verify HEAD와 빈 git diff만 조회. HEAD·실제 worktree 기준 준비 미검증. 비밀 파일은 열지 않았다.
결정: 사용자 코드/비밀 파일을 init/commit/stash하거나 제품 기능을 임의 구현하지 않는다.
다음 첫 행동: 사용자가 승인한 초기 commit 또는 기존 baseline을 확인한 뒤 사용자 변경/secret 비추적을 점검하고 adopt-baseline. 이번 명세 수정은 commit 승인으로 해석하지 않는다. 기존 blocker의 Git 기준 부재는 아직 해소되지 않았다.
상태는 taskctl show OPS-000로 다시 확인한다. handoff는 상태 원본이 아니다.
