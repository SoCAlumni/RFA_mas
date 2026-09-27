# P1-001E — 근거 없는 식별자 질의 수정

## 관찰

2026-09-27 main d7d5bbb에서 persona-regression-v2는 24 실행 중 23 통과,
PR2-colleague-evidence-insufficient 한 건 실패였다. 보안 실패는 0이다.
OMEGA 사내 식당 질의가 허용 문서의 일반 단어 `사내`만으로 근거 있음으로 판정됐다.
기존 R1/R2 규칙은 소문자 영어 동의어를 허용하려 모든 Latin 미일치도 허용했다.

## 수정 범위와 판단

권한이 적용된 문서 빈도만 사용한다. 명시적인 대문자 식별자(3자 이상)가
허용 문서에 없으면 R3으로 insufficient를 반환한다. 소문자 동의어, 알려진 식별자,
접속사 구분 및 파생 항목 순위 규칙은 유지한다. 의미 이해 전체를 보장하는 규칙은 아니다.
source: adapters/retrieval.py, tests/test_relevance_gate.py. 공통 계약/설정 변경 없음.

## 검증/다음 행동

아직 이 수정은 실행 전이다. 단위·검색·context 회귀를 실행하고 동일 Persona 24사례 및
최종 E2E golden 질의를 재실행한다. 결과는 attempt evidence와 최종 보고에 남긴다.
실패 수정은 최대 3회. fixture 기대값은 완화하지 않는다.
