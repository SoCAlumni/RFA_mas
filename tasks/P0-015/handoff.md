# P0-015 — 계약 scope 보강 전 조사 인계

아직 구현 전, feature worktree clean. 기존 API는 api-client/fixture owner와 하드코딩 membership을 주며 GET run owner 검사가 없다. coordinator 승인: 설치별 서버 생성 local-owner를 SQLite에 보존하고 기본 membership 없음, 설정된 API 인증 또는 실제 loopback peer만 데모 접근. body의 identity/thread/task 소유권은 신뢰하지 않는다.

SessionRecord/DirectWorkRequest 1.1을 사용하며 SessionMessage/SessionDetail/RunRecord 최소 DTO, 기존 WorkRepository 세션 메서드를 추가할 계획이다. Task 생성은 후속 P0-019로 유지하고 서버가 등록한 Task 소유권만 session과 N:M 연결한다. 기존 /v1/work 1.0 response를 유지한다. tests/test_graph.py의 무권한 service.get 호출은 principal을 명시하는 회귀로 바꾼다. 기존 DB legacy owner 미상 run은 제한 처리한다.

현재 제품 검증 not_run. 먼저 scope/계약 provider 보강을 수락한 새 generation claim을 받은 뒤 구현한다. 이후 immutable evidence/오류 로그/commit/submit 규약을 따른다. 원래 claim은 fencing만 하며 코드·사용자 파일을 삭제하지 않는다.
