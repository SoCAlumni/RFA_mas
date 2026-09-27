# NVIDIA 모델 호출 증거

이 문서는 NVIDIA hosted 추론 API 호출에 대한 실제 증거와 아직 실행하지 않은 범위를 구분해 기록한다. 확인일은 2026-09-26 KST다.

## 범위

P1-002A는 build.nvidia.com hosted endpoint에 **고정된 합성 한국어 요청만** 보내는 opt-in live smoke다. 저장소의 기존 `Settings`가 사용자 소유 `.env.dev`를 명시 경로로 읽어 `NVIDIA_BASE_URL`/`NVIDIA_MODEL`/`NVIDIA_API_KEY`를 사용한다. 사용자 노트, KB, 세션, trace 같은 제품 데이터는 전송하지 않는다.

이 절은 P1-002A 직접 smoke의 역사적 범위다. 이후 제품 `ModelPort` → bootstrap → graph 호출도 P1-002에서 실행했다(아래 제품 adapter 절). 2026-09-27에는 대표 E2E 3종×3회를 모델별 비교했다. Ultra/4096은 9/9 완료·30초 기준 통과, Lightning1024/4096은 7/9·8/9 완료이며 지연 기준에 실패했다. [최종 보고서](e2e-final.md)에 실패와 raw 측정을 함께 보존한다. 직접 smoke의 tool_call 제안은 도구 실행 성공이 아니다.

## 호출 대상

| 항목 | 값 |
| --- | --- |
| Endpoint | `POST https://integrate.api.nvidia.com/v1/chat/completions` (stream=false) |
| Model ID | `nvidia/nemotron-3.5-lightning-30b-a3b` (응답 model echo 동일) |
| 공식 참조 | [model card](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b/modelcard), [API reference](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-5-lightning-30b-a3b-infer) |
| 전송 제한 | https + `integrate.api.nvidia.com` host만 허용, redirect 미추종, 환경 proxy 미사용 |

## 관측한 지원 기능

| 기능 | 요청 조건 | 관측 결과 | 해당 AC |
| --- | --- | --- | --- |
| 한국어 근거 답변 | 기본 모드, temperature 0 | `finish_reason=stop`, "11월 14일, 김하늘 [E1]" — 사실 2개와 인용 일치 | AC1, AC3 |
| 근거 밖 질문 | 기본 모드 | "근거 부족" — 추측 없이 불확실 표시 | AC3 |
| 구조화 출력 | `response_format={"type":"json_object"}`, `chat_template_kwargs.enable_thinking=false` | JSON 객체가 `extra=forbid` Pydantic 모델 검증 통과(`evidence_ids=["E1"]`, `supported=true`) | AC2 |
| 도구 호출 제안 | named `tool_choice` (`lookup_schedule`) | `finish_reason=tool_calls`, 호출 1건, 인자 Pydantic 검증 통과. 테스트는 도구를 실행하지 않는다(실행 0회) | AC2 |
| 추론 출력 | 기본 모드 | `message.reasoning_content`가 content와 분리되어 반환된다. evidence와 trace에는 존재 여부만 남긴다 | — |

`json_schema`/strict 모드는 확인하지 않았으므로 지원한다고 주장하지 않는다.

## 실행 기록

| 실행 | 결과 | 관측 |
| --- | --- | --- |
| 탐색 호출 (개발, 비증거) | 성공 | 기본 모드 짧은 질문 13초, completion 341 token 중 대부분 reasoning. `enable_thinking=false` 일반 텍스트 1회는 97초 뒤 `finish_reason=length`, content "오늘" 한 단어로 비정상 — 표본 1회라 결론 내리지 않고 제한으로 둔다 |
| 개발 실행 1 (순차, 150초 timeout) | 2 passed / 2 failed | 근거 답변 75초, 도구 제안 7초 성공. 근거 밖 질문과 json_object가 `ReadTimeout`. 모델 오류가 아니라 hosted 지연 편차였다 |
| 개발 실행 2 (동시, 170초 timeout, 재시도 1회) | 4 passed | 전체 97.6초. 근거 답변 97.6초/1486 token, 근거 밖 24.4초, json_object 19.4초, 도구 제안 32.8초. 재시도 0회 |
| 공식 V1 attempt `v1-live-a1` | control evidence 참조 | `.agent/evidence/P1-002A/v1-live-a1/result.json` |

## 한계와 제품 연결 시 주의

- Hosted 응답 지연이 수 초에서 150초 이상까지 크게 달라진다. 제품 adapter는 전체 run deadline 안의 bounded timeout·재시도와 202 미완료 처리가 필요하다(P1-002).
- 기본 모드는 짧은 답에도 reasoning token을 많이 쓴다(짧은 답변에 completion 1325 token). 구조화 단계에는 thinking off + json_object 조합이 빠르고 짧았다.
- 한국어 품질은 합성 골든 2건의 결정적 문자열 검사만 확인했다. 품질 점수나 일반 성능 주장이 아니다.
- evidence 파일에는 요청/응답 SHA-256, 길이, 합성 응답 발췌, usage, latency만 저장한다. 키, Authorization header, reasoning 원문은 저장하지 않으며 쓰기 전에 키 포함 여부를 검사한다.

## 재현

```sh
env RFA_NVIDIA_LIVE=1 \
  RFA_NVIDIA_ENV_FILE=/absolute/path/to/.env.dev \
  RFA_NVIDIA_EVIDENCE_OUT=/tmp/rfa-nvidia-live-evidence.json \
  .venv/bin/python -m pytest -q -p no:cacheprovider tests/integration/test_nvidia_live.py
```

환경변수가 없으면 4개 모두 skip되며 skip은 증거가 아니다. 실제 API quota를 사용한다.

## P1-002 제품 adapter 구획

제품 경로: `MODEL_PROVIDER=nvidia`이면 bootstrap이 `NvidiaChatModel`을 `PublicOnlyEgressGate` 뒤에 조립한다. gate는 설정된 endpoint/model에만, 모든 근거가 public이고 질의·발췌에 비공개 표식이 없을 때만 전송을 허가한다. domain graph는 mock이 아닌 provider를 cloud endpoint로 보고, P1-005 screen으로 public이 아닌 근거를 먼저 제외한다. key/model이 없으면 `configuration_error`이며 mock으로 대체하지 않는다. 예산은 `NVIDIA_MAX_OUTPUT_TOKENS`와 domain task timeout을 쓴다. Run 예산 계약이 발행되기 전까지의 잠정값이다.

| 실행 (2026-09-27 KST, `wip/stack`) | 결과 |
| --- | --- |
| MockTransport 계약 `tests/test_nvidia_contract.py` | 42 passed. 네트워크·credential 없음. live 증거가 아니다 |
| opt-in live `tests/integration/test_nvidia_product_live.py`, n=1 | 1 passed. `WorkService.run` → domain graph → 관측 mode `real` → hosted chat completions. HTTP 시도 1회, 모델 호출 29.8초, Run 전체 30.3초, `completed`. 전송 본문에는 합성 공개 노트만 있었고 owner 전용 노트와 canary는 없었다. DRAFT 근거 audience는 public뿐이고 답에 합성 출시일이 들어 있었다. 증거 파일에는 길이·hash·latency만 저장하고 key는 저장하지 않는다 |

표본이 1회라 지연 분포나 품질을 주장하지 않는다. 검토 단계는 mock이며 외부 게시는 없다.
