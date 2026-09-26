# P0-018 — 승인 registry 기반 순수 TeamSelector

상태: owned 3개 파일 구현, 사전 독립 검토 반영, 필수 worker V1 통과; target 통합/done은 아직 아니다. shared DTO/ports/bootstrap/graph/설정은 수정하지 않았다.

인터페이스: TemplateRegistry.builtin() 또는 server-chosen from_file(path, approved_pins=trusted_mapping). TeamSelector(registry, grants={(subject_id, DomainId): frozenset(capabilities)}, available_capabilities, available_runtimes, budget_ceiling).select(SelectionRequest(goal, domain_id, outputs, requested_pattern?, requested_runtime?, budget?), TrustedPrincipal). 입력 intent에는 권한/capability/pin이 없다. Grants와 가용 runtime/capability는 서버 측 준비 결과이며 request나 LLM 출력에서 만들면 안 된다.

결과: SelectionDecision(status/reasons/template/definition_digest/roles/execution_budget/rejected). 승인 template 원본과 hash는 그대로, execution_budget이 request/server/template ceiling 교집합이다. P0-019 TeamFactory는 반드시 이 실행 예산을 적용하고 prepare 시 최신 권한/가용성을 재검사해야 한다. 반환 DTO 자체나 hash가 승인/권한 증명은 아니다. selector는 immutable grant/runtime 설정 snapshot을 사용하므로 정책 변경 시 신뢰된 최신 snapshot으로 재구성해야 한다.

규칙: benchmark=supervisor/paper_scout/experiment_runner/result_analyst, capability=evidence_search/experiment_run/result_analysis; research=supervisor/source_scout/evidence_reviewer, capability=evidence_search/evidence_review. 미구현 Engineering·미승인 version·수정된 definition·미가용 runtime·부족한 권한/예산은 제외. goal/output/capability/permission/runtime/총예산을 검사한 후보만 keyword-match count와 stable ID/version tie-break로 정렬한다. 간단한 저장 질의는 지원 goal이 아니므로 팀을 선택하지 않는다.

승인: source-controlled APPROVED_PINS가 fixture definition 전체(template/roles/goal/output/minimum budget)의 canonical SHA256과 일치해야 한다. fixture가 스스로 제공한 hash는 권한이 아니며 public constructor도 같은 pin 검사를 수행한다. 원문 JSON을 immutable snapshot으로 보관하고 반환 DTO는 재파싱해 분리한다.

사전 오류/수정: 독립 reviewer가 authenticated='yes'/1 coercion, budget bool/string/float coercion, public registry constructor의 pin 우회 세 건을 synthetic probe로 재현했다. 공통 DTO를 바꾸지 않고 selector의 strict boundary 검사/모든 registry 생성 경로 동일 검증으로 수정했다. 이후 큰 integer timeout에서 math.isfinite의 OverflowError를 확인하여 float에만 finite 검사를 적용하고 기존 DTO 범위 검사로 위임했다. 각각 음성회귀를 추가했다. 이미 기존 TeamBudget 생성자에서 정상화된 값의 원래 입력 타입은 복원할 수 없으며 현재 DTO 값만 판단한다; raw request dictionary는 coercion 전에 검사한다. formatter/lint 통과, 변경 중 미사용 import 1개는 제거했다.

범위 한계: 이 작업은 순수 선택 local 로직이며 worker 실행·Task당 team unique 제약·프로비저닝·router 연결·OpenShell/NVIDIA 실검증을 수행하지 않는다. 실제 agent/runtime capability 지원은 후속 P0-019/020가 제공·검증한다. 기능 flag나 선택 결과가 tool/data 권한을 추가하지 않는다.

검증: selector-01에서 계획된 .venv/bin/python -m pytest -q tests/test_team_selector.py = 53 passed in 0.05s, 기존 contracts/trace_eval 계약 회귀 64 passed in 0.66s, frozen/extended baseline check와 lint 통과. skip/실패/미수집 없음. 최종 독립 리뷰는 정상 Benchmark와 이전 4개 지적 및 미승인 OpenShell 경계의 tiny synthetic probe 통과를 확인했다. 이는 정식 pytest나 제품 E2E를 대체하는 검증이 아니며 각 결과를 별도 기록했다.

증거: .agent/evidence/P0-018/selector-01/source.json 및 result.json. 다음 첫 행동: scoped source commit → verifying submit. coordinator가 제출 source/증거를 검토하고 main에 통합한 후 V1과 필요한 회귀를 다시 실행해 close한다. WORK_LOG 및 evidence Git 기록은 coordinator가 관리한다. P0-019에 execution_budget 실제 적용·반환 decision 재검증·최신 정책 재조회 경계를 반드시 전달한다.
