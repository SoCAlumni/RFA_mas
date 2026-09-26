# NVIDIA 공식 Skill 제품 경로 증거 (P1-003)

**범위 표시: real, n=1.** 이 PC에서 공식 nemo-retriever Skill CLI 26.8.1을 제품 Research 경로로 실제 실행했다. embedding은 hosted `integrate.api.nvidia.com`이고 자료는 합성 PDF뿐이다. 모델 답변은 mock이며 실행하지 않았다. direct CLI 실행 증거(P1-003A)는 [Skill 증거](nvidia-skill.md)에 따로 있다.

## 결론

2026-09-27 03:14 KST에 opt-in live test를 1회 실행했고 **4 passed(11.25초)**였다. Research 팀 Run은 `completed`였다. source_scout는 ToolPort(`ObservedPort`, mode `real`)를 거쳐 공식 CLI를 호출했다. 결과는 3개 반환, 3개 사용, 0개 unmapped였다. top 근거는 source, page, revision을 보존했다. 결과 dump, trace, evidence 파일에는 credential이 없었다. Skill 결과는 설계대로 DRAFT 근거에 결합하지 않았다.

## 경로

```text
WorkService.run -> TeamRunner(research) -> source_scout
  -> ObservedPort(tool, mode=real) -> NemoRetrieverTool
  -> retriever query --format evidence
```

제품 설정은 `RETRIEVER_BACKEND=nemo_cli`다. KB 검색은 로컬 `LocalRetrieval`로 남고, 공식 Skill은 Research 팀 source_scout의 도구로만 추가된다.

## 실행 조건

| 항목 | 값 |
| --- | --- |
| 시각 | 2026-09-26T18:14:25Z(2026-09-27 03:14:25 KST), evidence 생성 시각 |
| 코드 | `wip/stack`의 P1-003 segment tip 68e36e2(P0-021 제외 rebase 전). 260f394에 포함된 c85efb3와는 bootstrap의 `_nvidia_config` 위치와 이 test의 not_run 문구만 다르다 |
| Skill | nemo-retriever CLI 26.8.1. 저장소 밖 별도 venv(`rfa_mas_worktrees/.tools/nemo-retriever-26.8.1`) |
| embedding | hosted `integrate.api.nvidia.com`(CPU host 기본값) |
| 입력 | 가상의 한국어 합성 PDF 2개(`synthetic_alpha_minutes`, `synthetic_security_policy`), index `rfa-synthetic-public-pdfs`, audience public |
| 질의 | "베타 출시일과 담당자 자료 조사"(Research 팀 goal) |
| 모델 | `MODEL_PROVIDER` 기본값 `mock` |
| credential | `RFA_NVIDIA_ENV_FILE`로 지정한 비공개 profile에서만 `NVIDIA_API_KEY`를 읽었다. 값은 출력·저장하지 않았다 |

## 명령

```bash
RFA_RETRIEVER_PRODUCT_LIVE=1 RFA_NVIDIA_ENV_FILE=/abs/path/.env.dev \
RFA_RETRIEVER_BIN=/abs/path/nemo-retriever-26.8.1/.venv/bin/retriever \
RFA_RETRIEVER_PRODUCT_EVIDENCE_OUT=/abs/tmp/p1003-product-live.json \
  .venv/bin/python -m pytest -q tests/integration/test_retriever_product_live.py
```

환경변수가 없으면 4개 test 모두 skip되며 skip은 증거가 아니다. fixture는 실패하면 hosted 호출을 다시 시도하지 않는다.

## 관측 결과

| 항목 | 값 |
| --- | --- |
| pytest | 4 passed, 11.25초 |
| ingest | `retriever ingest <PDF 2개> --method pdfium --lancedb-uri <tmp>/index/lancedb --table-name rfa-product-synthetic`, exit 0, 7.17초 |
| Research run | `completed`, 3.9초 |
| ingest+Research 경과 | 11.12초 |
| 역할(상태, tool 호출 수) | source_scout succeeded(2), evidence_reviewer succeeded(0), supervisor succeeded(0) |
| 외부 검색 | tool `nemo_retriever_query`, index `rfa-synthetic-public-pdfs`, status `succeeded`, `simulated=false`, 반환 3, 사용 3, unmapped 0 |
| tool 관측 mode | `real` |
| 근거 출처 | 3개 모두 `retriever:synthetic/synthetic_alpha_minutes`(p.2, p.1, p.3), revision `pdf-sha256-5323d8dd4f3bc670`, fidelity `verbatim`. content hash `77ccf435…`, `bf49e34b…`, `3baaf156…` |
| 기대 사실 | top 근거 "synthetic_alpha_minutes p.2"가 page 2, manifest의 source revision, audience public, fidelity verbatim을 유지하고 발췌에 합성 사실 "11월 14일", "김하늘"이 들어 있음(test pass) |
| DRAFT 결합 | Skill 근거는 DRAFT 근거에 없음(`draft_evidence_includes_external=false`, 설계) |
| credential | 결과 dump, trace, evidence 파일에서 credential 미검출(test pass). evidence JSON을 다시 검사해도 key 형태 문자열 0건 |

네 test는 다음을 확인했다: Research run 완료와 source_scout의 Skill 사용(`simulated=false`, unmapped 0), top 근거의 source/page/revision과 사실 보존, tool 관측 mode `real`과 DRAFT 비결합, credential 부재.

## not_run과 한계

- Skill 근거를 사용한 모델 답변: 이 smoke는 mock 모델을 쓴다. NVIDIA 모델 제품 경로는 별도 P1-002 smoke다([모델 증거](nvidia-model.md)).
- `nemo_service` backend(reserved), local embedding NIM.
- n=1이므로 지연 분포나 검색 품질은 주장하지 않는다. hosted embedding 지연은 실행마다 다를 수 있다.
- 실행 당시 evidence JSON의 not_run 문구는 "P1-002 ModelPort not wired"였다. 이 smoke가 mock 모델을 쓴 실제 이유와 맞지 않아 이후 test 문구를 정정했다.

## 원본 기록

evidence JSON과 pytest 출력은 임시 디렉터리에 생성됐고 저장소에는 커밋하지 않았다. 위 값은 그 두 파일에서 옮겼다.
