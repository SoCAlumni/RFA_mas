# NVIDIA 공식 Skill(NeMo Retriever) 실행 증거

이 문서는 NVIDIA 공식 Agent Skill `nemo-retriever`를 실제로 실행한 증거와 아직 실행하지 않은 범위를 구분해 기록한다. 확인일은 2026-09-26 KST다.

## Skill 출처와 역할

| 항목 | 내용 |
| --- | --- |
| 카탈로그 | https://build.nvidia.com/skills 에 `nemo-retriever` 카드가 SKILL.md와 같은 설명으로 등재되어 있고, 카드는 NVIDIA/skills 원본으로 연결된다 |
| 원본 | https://github.com/NVIDIA/skills/blob/main/skills/nemo-retriever/SKILL.md (skill-card의 skill version `e971432a`, 2026-09-11 commit) |
| 지시 내용 | `retriever` CLI 사용, 원격 NIM/service client는 `nemo-retriever==26.8.1` 설치, `retriever ingest` → `retriever query --format evidence`, 검색 근거만으로 답하고 source/page 보존 |
| 설치 | 저장소 밖 tool venv(Python 3.12)에 pinned nemo-retriever==26.8.1 (`retriever --version`: `26.8.1+g1992e3f.d20260825`). 저장소 `pyproject.toml`/`uv.lock`에는 추가하지 않았다 |

Skill은 agent가 NVIDIA 소프트웨어를 쓰는 **절차와 지침**이다. 이 실행에서 Skill 절차는 두 NVIDIA hosted API를 사용한다.

- 검색 embedding: CPU 호스트에서 CLI가 기본으로 `https://integrate.api.nvidia.com/v1/embeddings`의 `nvidia/llama-nemotron-embed-vl-1b-v2`(2048차원)를 호출한다.
- 근거 기반 답변: 검색된 evidence만 넣어 hosted `nvidia/nemotron-3.5-lightning-30b-a3b`를 호출한다. 이 추론 호출은 [nvidia-model.md](nvidia-model.md)의 P1-002A 증거와 별개로 이 Skill 흐름 안에서 다시 수행된다.

Skill 파일을 설치한 것만으로 성공으로 보지 않는다. 아래 결과는 CLI가 실제로 색인과 검색을 수행하고 hosted embedding에 의존함을 대조군으로 확인한 것이다.

## 실행 흐름

1. 가상 내용의 합성 한국어 PDF 2개(총 5쪽, `tests/integration/fixtures/retriever/`)를 임시 디렉터리에 복사한다.
2. `retriever ingest <pdf...> --method pdfium --lancedb-uri <tmp>/lancedb --table-name rfa-synthetic`. 텍스트 PDF이므로 OCR 대신 내장 텍스트를 쓴다.
3. 대조군: `NVIDIA_API_KEY` 없이 같은 query를 실행하면 "CPU-only ingest uses NVIDIA's hosted embedding endpoint and requires NVIDIA_API_KEY" 오류로 종료되어야 한다.
4. 골든 질문 3개를 `retriever query <질문> --top-k 3 --format evidence`로 조회해 top-1 citation, page locator, `fidelity=verbatim`, 사실 문자열을 검사한다.
5. 일정·보관 질문의 top-2 evidence만 context로 hosted Nemotron에 json_object(thinking off) 답변을 요청하고, Pydantic 검증과 함께 사실 3개와 인용이 검색 결과 citation 집합 안에 있는지 검사한다.

키는 기존 `Settings(_env_file=명시 경로)`에서 읽어 하위 프로세스 환경의 `NVIDIA_API_KEY`와 Bearer header로만 전달한다. proxy 환경변수는 전달하지 않고, 출력은 키 문자열을 가린 뒤 보관하며 evidence 파일은 쓰기 전에 키 포함 여부를 검사한다.

## 관측 결과

| 검사 | 결과 |
| --- | --- |
| ingest | exit 0, 2개 파일 → 5행, 5.4초 |
| 키 없는 대조군 | exit 1, hosted embedding에 NVIDIA_API_KEY 필요 오류 |
| "베타 출시일과 담당자는 누구인가?" | top-1 `synthetic_alpha_minutes p.2`, verbatim, "11월 14일"·"김하늘" 포함 |
| "3분기 GPU 예산은 얼마인가?" | top-1 `synthetic_alpha_minutes p.3`, verbatim, "1200만 원" 포함 |
| "회의 녹음 파일은 며칠 동안 보관하나?" | top-1 `synthetic_security_policy p.2`, verbatim, "90일" 포함 |
| 근거 기반 Research 답변 | "베타 출시일은 11월 14일이며, 담당자는 김하늘입니다. 회의 녹음 파일은 … 90일 동안 보관한 뒤 삭제합니다." 인용 `synthetic_alpha_minutes p.2`, `synthetic_security_policy p.2` — 모두 검색 결과 안 |

query 1회는 약 3초였다. 개발 실행 전체는 247초였고 대부분 hosted Nemotron 답변 대기였다(첫 시도 170초 timeout, 재시도 58초 성공).

| 실행 | 결과 |
| --- | --- |
| 탐색 (개발, 비증거) | 같은 fixture로 ingest 7.1초/5행, 세 질문 top-1 정답, 키 없는 query 실패 확인 |
| 개발 live 실행 | 7 passed, 247초. Research 답변 1회 ReadTimeout 후 재시도 성공 |
| 공식 V1 attempt `v1-live-a1` | control evidence 참조: `.agent/evidence/P1-003A/v1-live-a1/result.json` |

## 한계

- 합성 PDF 5쪽과 질문 3개에 대한 결정적 검사다. 검색 품질 점수나 대규모 한국어 성능 주장이 아니다.
- 이번 조건에서 score는 distance 값으로 낮을수록 가깝다. 여러 문서의 top-3에는 무관한 쪽도 포함되므로 제품은 top-1만 믿지 말고 근거 판단을 해야 한다.
- 로컬 GPU 모델, OCR/page-elements 원격 추출, 재순위화, agentic 모드, 배포된 Retriever service 경로는 실행하지 않았다.
- hosted Nemotron 지연 편차가 크다(P1-002A와 같은 관측). 제품 경로는 전체 deadline 안의 bounded timeout·재시도가 필요하다.

## 재현

```sh
uv venv /abs/tool/.venv --python 3.12
uv pip install --python /abs/tool/.venv/bin/python "nemo-retriever==26.8.1"
env RFA_RETRIEVER_LIVE=1 \
  RFA_NVIDIA_ENV_FILE=/absolute/path/to/.env.dev \
  RFA_RETRIEVER_BIN=/abs/tool/.venv/bin/retriever \
  RFA_RETRIEVER_EVIDENCE_OUT=/tmp/rfa-retriever-live-evidence.json \
  .venv/bin/python -m pytest -q -p no:cacheprovider tests/integration/test_retriever_live.py
```

환경변수가 없으면 7개 모두 skip되며 skip은 증거가 아니다. 실제 hosted embedding과 추론 quota를 사용한다. fixture는 `uv run --no-project --with reportlab==4.4.4 python tests/integration/fixtures/retriever/make_fixtures.py`로 다시 만들 수 있다.

## P1-003 제품 Research worker 구획

not_run. Research worker → ToolPort → 제한된 CLI/service adapter → `EvidenceBundle` 정규화와 audience/policy_version/source_revision 매핑은 P1-003 범위다. 이 문서의 direct Skill 실행을 제품 graph 경로 성공으로 해석하지 않는다.
