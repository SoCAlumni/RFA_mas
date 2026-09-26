# P1-003 — 공식 nemo-retriever Skill CLI를 Research ToolPort로 연결

## 출처와 역할

- 최초 wip는 다른 세션이다(914322c, 6b79237, 429c7dc; 메모 .agent/input/P1-003-wip-handoff.md).
- coordinator가 검토·rebase·통합했다(ledger_worker 보조).

## 구현

- wip/stack 커밋: 4bf65d2, 9bbb5b0, e587c03, c85efb3.
- NemoRetrieverTool(READ 전용)은 공식 Skill 절차대로 `retriever query … --format evidence`를 shell 없이 실행한다.
  - 환경변수는 최소한으로 넘기고 proxy는 넘기지 않는다.
  - timeout 시 프로세스 그룹 전체를 kill한다.
  - stdout은 1MB로 제한한다.
- 출처 매핑: operator가 등록한 index_manifest로 EvidenceItem에 연결하고 page locator를 보존한다. manifest에 없는 출처는 unmapped 수로만 센다.
- hosted embedding은 공개·합성 색인만 질의한다. 비공개 표식이 있는 질의는 CLI 실행 전에 거절한다.
- workers.py source_scout는 ExternalSearchBinding이 있을 때만 도구를 호출한다. 실패는 기록하고 로컬 KB로 계속한다.
- RETRIEVER_BACKEND=nemo_cli는 opt-in이며 기본값은 local이다. readiness는 잘못된 설정을 이름만으로 보고한다.

## 검증

- 개발: tests/test_retriever_contract.py 27 passed. 인접 테스트 227 passed.
- 실제(real, n=1, ledger_worker 실행): 제품 Research 경로 → 공식 Skill CLI, 4 passed(11.25s).
  - 합성 PDF 2개 ingest 7.17s(로컬 pdfium, hosted NVIDIA embedding).
  - Research run 3.9s. Skill 도구 관측 mode는 real. 결과 3개가 모두 매핑되었고 page·revision을 보존했다.

## 한계

- Skill 근거로 모델이 답변하는 경로는 not_run이다(smoke는 mock 모델).
- nemo_service 경로와 로컬 embedding NIM은 not_run이다.
- Skill 근거는 의도적으로 DRAFT에 결합하지 않는다.

