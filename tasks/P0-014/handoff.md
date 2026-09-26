# P0-014 — additive 1.1 contract handoff

목표/AC1~4: Session/제품 Task/Team, 추적·정책·근거·정확한 승인/게시 binding, 선택 context, 평가 상태를 기존 1.0과 분리한 1.1 Pydantic 단일 원본으로 구현했다. 수정 범위는 계약/ports/exporter/문서/합성 fixture/계약 tests뿐이다. 새 HTTP route나 실제 팀원 서비스 지원을 주장하지 않는다.

구현: 28개 additive DTO, version literal, Runtime prepare/cleanup·Retrieval load_context·Trace emit_event 선언. session↔task N:M, team role/memory binding, 승인 본문/첨부/대상/policy/ACL hash, null 단계 ID, unknown→조회, evaluator 규칙/Judge 상태 분리. 메서드의 제품 adapter 구현은 후속이다. 기존 1.0 JSON/fixture는 보존하며 source hash는 역사적 provenance, 현재 소스 hash는 extended.json에 있다.

검증: contracts-01은 14/34 테스트가 통과했으나 독립 리뷰에서 fixture ID/승인 상태·mutable draft hash 검사 공백이 재현되어 failed로 기록했다. 로그 기반 수정 후 contracts-02에서 V1 14 passed(0.03s), V2 50 passed(0.56s), 기존 계약 13 passed(0.29s). 리뷰 재실행 64 passed. lint 초기 포맷/exports/테스트 import 오류는 수정했고 최종 통과. serializer target 경고는 typed DraftTarget 생성으로 해소했다. 실패 원본은 덮어쓰지 않았다.

결정: ApprovalReference.matches는 model_copy/직접 할당 후에도 serialized DTO를 재검증하여 stale hash 승인을 거절한다. schema나 trace ID는 인증 증명이 아니며 transport/PolicyPort 원본 검증은 consumer에서 수행한다. local runtime은 sandbox가 아니다. 실험/trace의 NaN·Infinity를 거절한다.

증거: .agent/evidence/P0-014/contracts-01 및 contracts-02. 정상/오류 fixture 자체 EvalResult는 not_run이며 제품 실행 증거와 다르다. 제품 E2E/NVIDIA/팀원 live gate 미실행.

다음 첫 행동: coordinator가 scoped commit을 main에 통합 → 동일 V1/V2와 전체 회귀 → RFA-EXTENDED 1.1 digest publish-contract → 직접 consumer의 scope/선행/계약 수락. P0-015 세션과 독립 P1-006D trace를 우선하며 공통 local.py/config 소유권을 직렬 관리한다.

## Post-P0-015 revalidation — 2026-09-26

Explicit integrated_revalidation recovery issued generation2 from clean current main2f50bb4, preserving original submission/integration/evidence in attempts.approaches. No source rewrite. Current contract suites14+50passed; additive session DTO/API baseline digest3a6d70524d532866780288d2105c49f133fdd2a57e3309c783dd8a26a2aed3c3. New source evidence: post-sessions-01. Next: current-target integration verification then close; historical results remain historical, no full RFA E2E or real-service claim.
