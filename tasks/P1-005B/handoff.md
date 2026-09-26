# P1-005B — 피드백 4분류·scope·취소

- 구현(wip/stack 149baf7, 6fc8c86, d7b6a8b; quality_worker 개발)
  - migration 12: 피드백 item·revision·적용 로그.
  - style은 scope 안의 모델 참고 입력이다.
  - 개인 공개 선호는 비owner 초안의 withhold marker만 추가한다(domain disclosure_markers hook). 공유 범위를 좁힐 수만 있다.
  - 사실 정정은 tentative로 남고 KB에 쓰지 않는다.
  - 정책 변경 문장은 사용자가 다른 분류를 붙여도 proposal로 저장하며 official_policy_changed는 항상 false다.
  - scope 매칭과 적용 기록은 한 transaction이다. 취소 뒤에는 적용하지 않고 새 revision을 만든다.
  - `/v1/feedback` 경로, `GET /v1/runs/{id}/feedback`.
  - P1-004 feedback intent를 연결했다.
- 개발 검증: test_feedback 16 passed(M01 HTTP 흐름 포함). stack 구간 254 passed.

