# 결정 기록 (프런트 연동 API 작업)

형식: `D-<번호>: 질문 / 선택 / 이유 / 날짜`. 번복 시 번호를 언급하고 이유를 쓴다.

## 지시로 확정된 것 (선택지 없이 사용자 지시)

- **D-0.1**: admission queue·RAM 프로파일·Redis 를 이번 작업에서 뺄까? / 전부 `legacy/` 로 이동, `/ask` 는 동기(서버 타임아웃 180초), 202 경로 제거 / 프런트 연동 범위를 줄이고 계약을 단순하게 / 2026-09-27 (커밋 `9ab6d0b`)
- **D-0.2**: hosted LLM provider 를 어떻게 고를까? / `.env`(`RFA_LLM_PROVIDER=nvidia|gemini`, `RFA_LLM_MODEL`, `RFA_LLM_BASE_URL`)로 선택. 두 provider 모두 OpenAI chat.completions 로 호출하고 프록시가 요청 필드 allowlist·응답 정규화로 동일하게 맞춘다 (`proxy.upstream_payload` / `normalize_completion`). 두 프리셋 모두 reasoning 을 끈다(NVIDIA `enable_thinking=false`, Gemini `reasoning_effort=low` — `none` 은 3.5-flash-lite 가 400). Gemini 는 `GEMINI_MODEL` 로 `gemini-3.5-flash-lite`(기본) | `gemini-3.8-flash` 두 모델만 제공, nvidia 는 `NVIDIA_MODEL`. 라이브 확인: 프록시 경유 internal 1.6초 allow, external 2.9초 요청 마스킹 4건 / 사용자가 Gemini API 로도 호출하길 원함 / 2026-09-27
- **D-0.3**: 검열 LLM 판정이 턴마다 3~4회, hosted 호출 1/4 이 45초까지 멈춤 → 어떻게 줄일까? / 요청 측 판정은 가장 새로운 non-system 메시지 1회, 응답 측은 regex 만, 최종 답변 판정 1회 유지. 타임아웃 45→12초 + 1회 재시도, 두 번 다 실패하면 그 텍스트는 regex 결과로 통과(`on_error: allow`, 감사에 degraded) / "호출이 안 되면 해당 provider 를 빼버려" — 사용자 지시. 실측: summarizer 턴 53초 → 12초 / 2026-09-27

## 대화로 정할 것

- **D-1 스트리밍 방식**: 토큰 스트리밍(게이트웨이 HTTP) vs 문장 단위 의사 스트리밍? / **둘 다, 병렬 트랙**. SSE 계약은 A(의사 스트리밍: `run.started → stage → delta… → guard.final → done`, 봉투 `{type,runId,seq,ts,data}`)로 지금 구현하고, B(샌드박스 게이트웨이 `/v1/chat/completions` HTTP 전송)는 브로커 전송 교체로 병렬 진행. 성공하면 delta 공급원만 교체(owner 만 토큰, guest 는 검열 후) / 실측: 턴 14.8초 중 모델 호출 4.2초, 나머지 ≈10초가 `nemoclaw agent` CLI 경로 오버헤드 → B 는 스트리밍보다 지연 제거가 본질. 사용자: "그렇게 오래 걸릴 작업이 아니다, 병렬로" / 2026-09-28
- D-2 ~ D-9: 미착수.
