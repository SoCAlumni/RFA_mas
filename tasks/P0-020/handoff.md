# P0-020 — 역할별 실행·Supervisor 수집·팀 예산·취소

## 구현 사실 (commit 86d770f, base a92912c)

- `application/workers.py`: TeamRunner.ensure_and_bind(TeamFactory.ensure, Run 기준 key `run-team:<run_id>`, 동시 재생 시 같은 팀을 짧게 대기) → repository.bind_run_team(원자·멱등, 다른 Task/팀 재결합 거절) → execute(역할 순서 Benchmark: paper_scout→experiment_runner→result_analyst→supervisor, Research: source_scout→evidence_reviewer→supervisor).
- 역할 실행은 RuntimePort(LocalRuntime, task_type `team_role`, TaskRequest.run_id=역할 key)로 수행한다. handler는 등록된 member spec(agent_id·memory namespace)과 일치할 때만 실행한다.
- SupervisorBus: worker→supervisor/supervisor→worker만 허용, worker 간 직접 통신은 direct_message_denied.
- 권한: 역할별 서버 allowlist(검색: paper_scout/source_scout/experiment_runner; 도구: experiment_runner=benchmark_log_parse, result_analyst=metric_compare). 검색은 ACL-bound RetrievalPort와 target audience cap 교집합만 사용한다.
- 예산: 팀 전체 step/tool/time/concurrency를 역할 간 공유. 토큰 상한은 provider usage 없으면 budget_unavailable. 필수 역할 실패·예산 초과 시 부분 결과(TeamRunResult status partial/failed) 저장, review 미시작. 취소 barrier는 다음 역할/도구를 차단하고 LocalRuntime.cancel이 현재 역할을 중단한다.
- migration6: run_team_bindings, role_executions(재시작 시 running→unknown, 재실행 금지), team_run_results. WorkService.team_result/cancel.
- 관측: ObservedPort.execute(tool 경계), 저장된 팀 멤버만 `team-member:<role>` actor alias, team_id는 결합 뒤에만. domain 경로 검색 audience를 agent cap과 교집합(owner target의 private 노트로 인한 draft_binding_mismatch 수정).
- 실험 수치는 합성 로그 파싱(mode fixture_log_parse, simulated_experiment=true)이며 실제 GPU/모델 실측이 아니다. 역할은 모델을 호출하지 않아 token은 null이다.

## 검증

- tests/test_team_execution.py 9개(순서·Supervisor 경유·수치·trace/canary, research/일반 질의 팀 미생성, 후속 Run 팀 재사용·동시 ensure 멱등, 역할 도구/검색/메모리/직접 통신/위조 spec 차단, 공유 예산·필수 역할 실패, 토큰 fail-closed, 취소 barrier, 재시작 unknown 비재실행).
- claim 전 개발 확인: 제품 615 passed(taskctl/task_migration 제외).

## 제한·다음

- 병렬 역할 실행, HTTP 취소 API/UI, 실제 OpenShell/NVIDIA 모델 역할은 범위 밖(P0-025/P1-008/P1-007).
- 다음: RFA-EXTENDED 새 digest 발행 → consumer 수락, P1-004(intent routing)·P1-004A.

## 재검증 post-ops005 (2026-09-26T16:41Z)

- 사유: OPS-005 changed TASK_EXECUTION_RULES.md (global context ref) at 4bf0ec9; direct dependencies (transitive) of next claims only
- 소스 변경 없이 현재 통합 HEAD ab9cf68에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-004B (2026-09-26T16:55Z)

- 사유: Direct/transitive dependencies of P1-004B before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 0bdef6a에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P2-003 (2026-09-26T17:06Z)

- 사유: Direct/transitive dependencies of P2-003 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD d3cad28에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-005 (2026-09-26T17:16Z)

- 사유: Direct/transitive dependencies of P1-005 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 0161cc2에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-021 (2026-09-26T18:05Z)

- 사유: Direct/transitive dependencies of P0-021 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 2223a7f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-008 (2026-09-26T18:21Z)

- 사유: Direct/transitive dependencies of P1-008 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3c5cb6f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-022 (2026-09-26T18:36Z)

- 사유: Direct/transitive dependencies of P0-022 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3466bc5에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-024 (2026-09-26T19:52Z)

- 사유: Direct/transitive dependencies of P0-024 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 501aebf에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-005B (2026-09-26T20:14Z)

- 사유: Direct/transitive dependencies of P1-005B before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 68d474f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-020A (2026-09-26T20:39Z)

- 사유: Direct/transitive dependencies of P0-020A before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD d962b8a에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-020B (2026-09-26T20:42Z)

- 사유: Direct/transitive dependencies of P0-020B before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 1a1e59f에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-025 (2026-09-26T21:05Z)

- 사유: Direct/transitive dependencies of P0-025 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 90569fd에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P0-005A (2026-09-26T21:30Z)

- 사유: Direct/transitive dependencies of P0-005A before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 71bf2af에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-008E (2026-09-26T21:50Z)

- 사유: Direct/transitive dependencies of P1-008E before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 3728423에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-004C (2026-09-26T22:09Z)

- 사유: Direct/transitive dependencies of P1-004C before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 81b1c08에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-002 (2026-09-26T23:12Z)

- 사유: Direct/transitive dependencies of P1-002 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 88c33d9에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-003 (2026-09-26T23:32Z)

- 사유: Direct/transitive dependencies of P1-003 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 8b06ac8에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 pre-P1-007 (2026-09-27T00:01Z)

- 사유: Direct/transitive dependencies of P1-007 before claim (OPS-005)
- 소스 변경 없이 현재 통합 HEAD 71b2d09에서 계획된 검증을 worker/target 단계로 재실행한다.

## 재검증 final-local-unlock (2026-09-27T01:33Z)

- 사유: NAT reservation is closed; finish remaining source-dependent tasks once, without extra concurrent claim runners or product edits
- 소스 변경 없이 현재 통합 HEAD 93ae0a3에서 계획된 검증을 worker/target 단계로 재실행한다.
