# OPS-001 — 명세 보강 후 재검증 중

기존 migration-final-02의 254 통과 기록과 최초 이관 결과는 불변 보존한다. 이번에는 제품 src/설정/lock을 변경하지 않고 task 6개와 명세/공통 문서를 보강했다. 기존 기반 done 13개를 유지한다.

technical-spec-01: 66개 task validator와 운영·명세 73개 테스트(29.27s)는 통과. 전체 offline suite는 231 passed/8 failed/17 errors(32.61s)로 실패했다. 기본 /private/tmp의 SQLite disk I/O error와 Ruff cache ENOSPC를 관찰했으며 성공으로 처리하지 않는다. schema/9 fixture shape 검사도 통과. NAT·새 KB·레드팀 제품 AC는 not_run이다.

접근 변경 근거: 새 mktemp TMPDIR에서 같은 SQLite 실패 테스트를 실행하니 1 passed(0.01s). 사용자 파일 삭제·제품 코드 수정은 하지 않았다. 이전 attempt들과 이번 실패를 보존하고, explicit isolated TMPDIR를 준비 조건에 추가한 새 spec/attempt로 전체 suite를 확인한다. 수정/검증 한도 시점의 재개 이유는 이 환경 차이이며 같은 조건의 무근거 반복이 아니다.

다음 첫 행동: 운영 audit V1 prerequisite에 명시 TMPDIR와 no-secrets environment를 고정하고 새 source manifest/attempt에서 재검증. 다시 실패하면 추가 무의미한 반복 없이 환경 blocker와 인계 기록을 남긴다. 실제 baseline은 여전히 HEAD 없음/미등록(OPS-000)이다.
