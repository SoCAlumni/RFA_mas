# P1-005 — 대상별 DRAFT·읽기/공유/외부 전송 정책

## 구현 사실 (16b8560, P1-001B 위)

- domain graph: owner/public local 대상은 P1-001B staged loader(L0 manifest→L1→예산 내 L2)로 context를 구성하고 통계·insufficient를 ModelRequest와 TaskResult(context)에 남긴다. 그 외 대상/엔드포인트는 ACL-bound retrieval.
- 모델 호출 전 share_egress_filter: 대상에 공유 불가 audience, 비-owner 대상의 비공개 marker(합성 canary + owner 공개 선호 marker hook), cloud endpoint의 비공개 항목을 통째로 보류(문자열 삭제 아님). 공개 대상 요청문 자체에 비공개 marker가 있으면 policy_denied. DRAFT는 통과한 근거만 인용.
- bootstrap: 신뢰된 staged-context factory(retrieval 관측 유지), model endpoint(mock=local, 그 외 cloud).

## 검증

- tests/test_policy.py 신규 6(공개 초안 공유·screen·loader, context 통계/insufficient, 공개 요청문 marker 거절, egress 매트릭스 local/cloud) + graph/trace/resume/api 회귀 88 passed(개발 확인). P1-006E RT03(public 라벨에 섞인 canary의 모델 전달) 결함을 해소하는 경로.

## 제한

- 개인 공개 선호 marker 저장은 P1-005B. BoundAccess는 owner/public local만 지원하므로 company/BU 대상은 retrieval 경로+screen.

