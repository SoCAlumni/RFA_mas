# NVIDIA 모델 호출 증거

이 문서는 NVIDIA hosted 추론 API 호출에 대한 실제 증거와 아직 실행하지 않은 범위를 구분해 기록한다. 확인일은 2026-09-26 KST다.

## 범위

P1-002A는 build.nvidia.com hosted endpoint에 **고정된 합성 한국어 요청만** 보내는 opt-in live smoke다. 저장소의 기존 `Settings`가 사용자 소유 `.env.dev`를 명시 경로로 읽어 `NVIDIA_BASE_URL`/`NVIDIA_MODEL`/`NVIDIA_API_KEY`를 사용한다. 사용자 노트, KB, 세션, trace 같은 제품 데이터는 전송하지 않는다.

제품 `ModelPort` → bootstrap → graph 경로의 NVIDIA 호출은 아직 **not_run**이다. 그 adapter(`src/rfa_mas/adapters/nvidia.py`)와 정책·예산 연결은 P1-002 범위이며, 이 문서의 결과를 제품 경로 성공으로 해석하지 않는다.

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

not_run. P1-002가 MockTransport 계약 테스트와 bootstrap 조립을 완료한 뒤, 제품 경로 live 확인을 이 아래에 별도로 추가한다.
