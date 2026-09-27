# P1-006F 통합 인계

기존 8f80676 구현을 회수한다. 무료 Langfuse의 서버 retention 부재를 앱 소유 trace만
만료 조회→삭제→재조회하는 선택적 CLI로 처리한다. 설정/권한 없음은 요청 0회,
미확인 삭제는 partial이며 다른 앱/보존 기간 안의 trace를 유지한다.
CLI는 자동 예약·실제 운영 데이터 삭제를 수행한 것으로 표시하지 않는다.

기록된 live 결과: 2026-09-26T22:41~22:46Z, 로컬 Langfuse 4.46,
6 tests passed. 실제 clock 삭제 0, 주입 clock +8일에서 앱 trace 2건 삭제·조회 소멸,
다른 앱 trace 유지. MinIO raw-upload 삭제는 별도 전용 stack 검증이며
일반 운영 데이터의 자동 삭제 권한으로 확대하지 않는다.
현재 통합의 unit/security/settings 결과는 불변 task evidence를 따른다.
다음 행동: 최종 전달 문서에 수동 dry-run/일일 실행 방식과 실제 검증 한계를 표시한다.
