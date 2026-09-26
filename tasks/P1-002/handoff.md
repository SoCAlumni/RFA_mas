# P1-002 — NVIDIA ModelPort adapter

## 출처와 역할

- 최초 wip는 다른 세션(5f0afb1)이다. 그 세션이 2시간 이상 유휴 상태여서 coordinator가 검토·rebase·통합했다(ledger_worker 보조).

## 구현

- wip/stack 커밋: fba3900, 21f1c43, 4e26e79.
- httpx 기반 async NVIDIA chat/completions adapter.
  - `stream=false`로 호출한다.
  - reasoning은 content와 trace에 복사하지 않는다.
  - 응답 크기를 제한한다.
  - redirect와 ambient proxy를 쓰지 않는다(follow_redirects=false, trust_env=false).
  - 429와 일시적 5xx는 bounded Retry-After로 처리한다(최초 포함 최대 3회).
  - 잘못된 JSON은 오류로 처리하고, 오류 문구에 비밀을 넣지 않는다.
- bootstrap 배선: model_provider=nvidia일 때 정확한 endpoint·model만 허용하는 public-only egress gate를 둔다.
  - 선택했는데 키나 모델이 없으면 configuration_error로 실패한다. mock fallback은 없다.
  - 기본값은 mock이며 키 없이 부팅한다.
- P0-025 readiness의 model:nvidia가 실제 상태를 보고한다. 부족한 설정은 변수 이름만 표시한다.

## coordinator 결정 (unresolved 해소)

- egress: P1-005 graph screen과 adapter decorator gate, 두 겹의 신뢰 경계로 확정했다. 새 DTO는 없다.
- budget: 호출당 deadline, bounded 재시도, output token 상한, 그래프 step/tool 예산을 적용한다. 팀 전체 token 예산 전파는 후속이며 이 task에서 주장하지 않는다.

## 검증

- 개발: tests/test_nvidia_contract.py 42 passed(MockTransport). 인접 테스트 172 passed.
- 실제(real, n=1, ledger_worker 실행): tests/integration/test_nvidia_product_live.py.
  - 경로: Work service → domain graph → NVIDIA chat completions. HTTP 시도 1회.
  - model call 29,784.5ms, run 30,305.4ms, status completed. model 관측 mode는 real.
  - 공개 합성 note만 전송했고 owner canary는 전송하지 않았다.
  - 키는 명시 env file(.env.dev)로만 쓰고 값은 출력하지 않았다. 수치는 docs/evidence/nvidia-model.md에 있다.

## 한계

- egress/budget seam은 provisional이다(NVIDIA_MAX_OUTPUT_TOKENS 기본 1024).
- live는 n=1이다. 품질 평가는 P1-006A 소관이다.

