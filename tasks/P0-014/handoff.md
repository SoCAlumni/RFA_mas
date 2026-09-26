# P0-014 — 확장 계약 동결 재개

목표/관련 AC: 기존 Session/Task/TeamSpec/Schedule 계획에 Execution/Trace·Evidence/Context·Policy·Draft/Approval/Publish·Eval 최소 계약을 함께 동결한다(AC1~AC4, V1/V2).

현재 구현 사실: schema 1.0의 WorkRequest/AgentSpec/EvidenceItem/Ref/Bundle, PolicyDecision.allowed/code, DraftBundle.version/content_hash/target, ReviewDecision, EvaluationCase/EvalResult가 있다. 새 entity·policy decision ID·stage-aware 조회·관측 allowlist는 없다. RuntimePort는 run/status/cancel만 있다. 기존 1.0 baseline과 9종 fixture의 과거 증거는 보존되어 있다.

이번 변경 파일/범위: 이 task.yaml과 관련 설계/명세만 수정. 제품 src, DTO export, pyproject/lock, .env.example은 변경하지 않았다. 예정 trace_eval_cases.json/test_trace_eval_contract.py/extended.json은 아직 없다.

결정/이유: 기존 Pydantic 단일 원본을 확장한다. session↔Task N:M, Task당 활성 팀 하나. 미래 단계 ID는 null. trace ID/LLM allow는 권한이 아니다. 읽기·공유·endpoint 전송은 별도 판단. 승인/게시 원본 승희, runtime 원본 다영, Policy 최종 소유 미확정. repository-local reference이며 상대 지원을 뜻하지 않는다.

제공할 인터페이스: planned RFA-EXTENDED 1.1에 정상/deny/승인 없음/승인 후 변경/source ACL 변경/replay/unknown fixture, typed trace allowlist, 선택적 KB stage 조회 및 평가 관찰 결과를 연결한다. 기존 allowed/code와 상태 enum 호환성·migration을 먼저 기록하고 schema/fixture 검증 후 publish-contract한다. 미지원 version은 명시 거절. trace export 행위 검증은 P1-006D, consumer는 P1-008이 담당한다.

검증/증거: 이번 조사·명세 정합성은 제품 AC 증거가 아니다. 새 AC는 전부 not_run, status=todo. 기존 P0-001~013 done/evidence를 재개방하지 않는다.

Blocker: Git 초기화는 됐지만 HEAD 없음·프로젝트 untracked·integration baseline 미등록. OPS-000은 여전히 필요하다. 이 요청만으로 commit/stash하지 않는다.

정확한 다음 첫 행동: taskctl status와 context-pack P0-014 → OPS-000의 승인된 baseline 확인 → 유효한 claim 획득 → docs/EVALUATION_CONTEXT.md A 및 contracts/models.py의 해당 DTO를 읽고 필드/optional/null/버전 migration 표를 확정 → 단일 DTO와 7종 fixture 구현. 새 digest를 발행한 뒤 consumer의 unresolved/spec_revision을 갱신한다. NAT 호환성 spike(P0-027)는 별도 결과이며 계약 작업을 막지 않는다.
