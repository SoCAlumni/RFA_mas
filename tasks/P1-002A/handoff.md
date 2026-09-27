# P1-002A handoff

- 목표 / 관련 AC: build.nvidia.com hosted NVIDIA 모델 실제 호출 증거. AC1 합성 요청 성공, AC2 json_object Pydantic 검증과 named tool_choice 제안(실행 0회), AC3 한국어 근거 답변·근거 밖 '근거 부족'.
- 현재 구현 사실: spec r2(사용자 우선순위 변경)로 P1-002 의존을 제거하고 direct hosted synthetic smoke로 한정했다. src/ 제품 코드는 변경하지 않았다. 제품 ModelPort/bootstrap/graph 경로의 NVIDIA 호출은 not_run이며 P1-002 범위다.
- 변경 파일 / worktree / branch / commit: tests/integration/test_nvidia_live.py, docs/evidence/nvidia-model.md, docs/EDUCATION_MAPPING.md — rfa_mas_worktrees/P1-002A, task/P1-002A, a3055c9 (base 47f002f).
- 결정과 이유: 4개 합성 요청을 모듈당 1회 동시 전송, 요청 timeout 170s·timeout/429/502/503/504만 1회 재시도. https+integrate.api.nvidia.com만 허용, follow_redirects/trust_env=false. 키는 Settings(_env_file=명시 경로) SecretStr 안에만 있고 evidence 작성 전 포함 여부를 검사한다. reasoning_content는 존재 여부만 기록.
- 실제 검증: worker attempt v1-live-a1 — 4 passed 38.67s, 재시도 0, model echo 일치. 이전 개발 실행: 순차 150s timeout에서 2 passed/2 ReadTimeout(hosted 지연), 동시 실행 4 passed 97.6s. 탐색 1회에서 enable_thinking=false 일반 텍스트가 length로 잘린 비정상 응답 — 표본 1회라 제한으로만 기록.
- 실패/blocker/side effect: 외부 부작용 없음(읽기 전용 추론 호출). API quota 소량 사용.
- integration 예약: submit 후 integration pending. 대상 파일은 P1-001A 예약과 겹치지 않는다.
- 다음 첫 행동: coordinator가 task/P1-002A(a3055c9)를 main에 반영하고 target에서 동일 V1 argv로 integration evidence를 새 attempt로 기록한 뒤 integrate/close. 제품 경로 live 확인은 P1-002 완료 후 docs/evidence/nvidia-model.md의 P1-002 구획에 추가.

## 재검증 reval1 (2026-09-26)

- 사유: P1-003A 통합이 공동 소유 파일 docs/EDUCATION_MAPPING.md를 변경해 P1-002A 증거가 stale이 되었다.
- 소스 변경 없이 main a92912c에서 같은 live V1을 worker/target 단계로 재실행한다.

- reval1-worker(a92912c) 4 passed 275.6s(2건 timeout 후 재시도 성공)였으나 main이 462bd2f로 이동해 submit 거절. reval2-worker(462bd2f) 4 passed 69.2s였으나 claim baseline이 a92912c라 scope 검사에서 P0-020 변경이 섞여 거절.
- 기준점 재설정 recover는 선행 P0-017이 P0-020 계약 발행 뒤 verifying/stale이라 거절됨.
- 다음 첫 행동: P0-017이 다시 done이 되면 main HEAD로 worktree를 ff한 뒤 recover(integrated_revalidation, disposition resume)로 기준점을 현재 HEAD에 맞추고 새 attempt로 worker/target live V1 재실행 후 submit/integrate/close.


## 2026-09-27 coordinator takeover

- worker-nvidia-1 claim(gen 2)은 15:01Z 만료, 해당 세션은 약 15:49Z 이후 유휴. 그 worktree가 claim baseline(a92912c) 이후로 FF되어 submit이 범위 밖 변경으로 막힌 상태였다(reval2-worker 통과 증거는 미제출).
- coordinator가 recover(resume)로 baseline a92912c의 새 worktree(P1-002A-r3)에서 이어받아, 인수 기록을 소유 증거 문서에 남기고 live V1을 worker/target 단계로 다시 실행한다.

## 재검증 freeze-reval3 (2026-09-27T00:29Z)

- 사유: Revalidate after P1-007 batched integration and shared-file changes (hook incident restored); no source change in these tasks
- 소스 변경 없이 현재 통합 HEAD a6c271d에서 계획된 검증을 worker/target 단계로 재실행한다.
