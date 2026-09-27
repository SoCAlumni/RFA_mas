# P1-003A handoff

- 목표 / 관련 AC: build.nvidia.com Skills 카탈로그의 NVIDIA 공식 nemo-retriever Skill 실제 실행 증거. AC1 CLI ingest/query 실제 수행+키 없는 대조군 실패, AC2 top-1 source/page와 근거 기반 Research 답변, AC3 Skill 출처·역할 기록.
- 현재 구현 사실: spec r2(사용자 우선순위)로 P1-003 의존을 제거하고 direct Skill smoke로 한정했다. src/ 제품 코드·pyproject/uv.lock은 변경하지 않았다. 제품 Research worker→ToolPort→EvidenceBundle(audience/policy/source_revision) 경로는 not_run이며 P1-003 범위다.
- 변경 파일 / worktree / branch / commit: tests/integration/test_retriever_live.py, tests/integration/fixtures/retriever/(합성 PDF 2개+생성 스크립트), docs/evidence/nvidia-skill.md, docs/EDUCATION_MAPPING.md — rfa_mas_worktrees/P1-003A, task/P1-003A, f4aa90f (base 3a1a5ee).
- 결정과 이유: nemo-retriever==26.8.1을 저장소 밖 tool venv(rfa_mas_worktrees/.tools/nemo-retriever-26.8.1)에 설치해 공유 의존성 파일을 건드리지 않았다. 텍스트 PDF라 --method pdfium. CPU 호스트는 hosted integrate.api.nvidia.com embedding(llama-nemotron-embed-vl-1b-v2, 2048차원)을 쓰며 torch/transformers/vllm 미설치를 확인했다. 키는 Settings(_env_file) → 하위 프로세스 NVIDIA_API_KEY/Bearer만, proxy 미전달, 출력 redaction과 evidence 키 검사.
- 실제 검증: worker attempt v1-live-a1 — 7 passed 51.67s, ingest 5행, 대조군 exit1, 3질문 top-1 정답, Research 답변 30.6s 인용 검색 결과 안. 개발 실행 7 passed 247s(Research 1회 ReadTimeout 후 재시도 성공).
- 실패/blocker/side effect: 외부 부작용 없음(hosted embedding/추론 읽기 호출). 임시 LanceDB는 pytest tmp에만 생성.
- integration 예약: submit 후 pending. 대상 파일은 다른 task 예약과 겹치지 않는다.
- 다음 첫 행동: coordinator가 f4aa90f를 main에 반영하고 target에서 같은 V1 argv로 새 integration attempt를 기록한 뒤 integrate/close. 제품 경로는 P1-003에서 이 CLI/evidence 형식을 adapter 입력으로 재사용.

## 재검증 final-live (2026-09-26T22:55Z)

- 사유: Live Skill smoke revalidation after all product integrations; old worktree diverged from main
- 소스 변경 없이 현재 통합 HEAD 88c33d9에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 freeze-reval3 (2026-09-27T00:30Z)

- 사유: Revalidate after P1-007 batched integration and shared-file changes (hook incident restored); no source change in these tasks
- 소스 변경 없이 현재 통합 HEAD a6c271d에서 계획된 검증을 worker/target 단계로 재실행한다.
