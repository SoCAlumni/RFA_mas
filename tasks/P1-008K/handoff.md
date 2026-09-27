# P1-008K — 실제 LLM 응답·추론

## 구현 사실
- settings: ALLOW_EXTERNAL_EGRESS의 model_egress 소비자 구현. MODEL_PROVIDER=nvidia일 때만 유효(external_egress_effective, scope=model_endpoint_owner_context); mock에서는 여전히 reserved. doctor에 external_egress_scope 추가. .env.example 설명 갱신.
- adapters/nvidia.py: OwnerConsentEgressGate(소유자 대상만 비공개 근거 허용, 공개 대상은 PublicOnly 규칙), NvidiaChatModel.reason(system,user)→JSON(동의 gate 전용, 동일 재시도/오류 코드). graphs/domain.py: share_egress_filter(private_egress) — 동의+소유자 대상일 때만 cloud endpoint에 non-public 통과. bootstrap: gate 선택·deps.private_egress·TeamRunner.model 주입.
- workers.py: supervisor 요약을 실제 모델(reason)로 합성(소유자 대상·실제 모델일 때만; placeholder/짧은 출력은 무시→결정적 기록 유지), 역할별 기록을 뒤에 보존, output.llm 메타.
- PoC: --model nvidia --env-file(NVIDIA_* 값만 복사, 누락 시 poc_model_not_configured 종료), 상태 API에 model 정보(어댑터/simulated/model_id/egress). chat: reasoning 단계(LLM 의도·담당 제안, 후보 밖 ID 무시, 파괴적 표현 보호, 실패 시 규칙 진행), synthesis 단계(비서 fallback LLM 답변; 사용 source/revision과 함께 reply_json 보존, 재조회 시 revision 불일치/접근불가면 미표시), team_result에 Supervisor LLM 메타. UI: 실제 모델 배지·단계 detail·답변 모델 표시.

## 검증
- tests/test_chat_llm.py(6): settings 동의 범위, filter/gate 소유자 대상 한정, reason() gate 필수, PoC 설정 오류, 실제 어댑터+가짜 transport로 채팅 e2e(LLM 의도 저장/합성 답변/revision 변경 시 미표시/공개 초안에 비공개 미전송/후보 밖 ID 무시/파괴적 보호/Supervisor 요약). scoped 145 passed + 팀/레드팀/평가 142 passed.
- 실제 호출(.env.dev, 18790, 합성 data-dir): 메모 저장(LLM intent)→질의에 nemotron 답변(simulated=false)→검증 요청으로 Task 팀 생성+Supervisor LLM 요약(합성/실측 구분 유지)→후속 질의 LLM 담당 선택→무관 질문 clarify. 1회 모델이 "summary" placeholder를 반환→길이/placeholder 가드 추가 후 재확인. 관찰: .agent/evidence/P1-008K/live/observation.json(키·endpoint 값 없음).
- 운영: claim lease 만료→resume recover가 stale 예약 충돌로 거절→discard 후 재claim은 최신 HEAD 요구→새 int worktree에 cherry-pick. frozen 파일 변경은 Freeze-Override trailer로 기록.

## 다음 행동
int worktree 검증·submit→override merge→target 검증→close→사용자 8780을 --model nvidia로 재시작.
