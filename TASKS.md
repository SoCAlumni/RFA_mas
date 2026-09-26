# 개발 작업 현황 — 자동 생성

직접 수정하지 마세요. `tasks/<ID>/task.yaml`만 원본이며 `taskctl`로 갱신합니다.

[공통 맥락](docs/PROJECT_CONTEXT.md) · [실행 규칙](TASK_EXECUTION_RULES.md) · [이관 기록](docs/TASK_MIGRATION.md) · [요구사항/일정](docs/PRODUCT_REQUIREMENTS.md)

Canonical control root: `/Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas`. Git baseline: **registered**.

개발 작업 완료와 제품 최종 gate는 별개입니다. mock 통과는 실제 NVIDIA/Skill/NemoClaw/OpenShell 증거가 아닙니다.

| ID | 작업 | 우선순위 | 상태 / 검증 | stream | h | 선행 |
| --- | --- | --- | --- | --- | --- | --- |
| [OPS-000](tasks/OPS-000/task.yaml) | 사용자 변경을 보존한 Git/worktree baseline 등록 | P0 | verifying / stale | contracts/control | 0.5 | — |
| [OPS-001](tasks/OPS-001/task.yaml) | 작업 이관·공통 CLI·병렬 제어 검증 | P0 | verifying / stale | contracts/control | 0.5 | — |
| [OPS-002](tasks/OPS-002/task.yaml) | 후속 통합 후 재검증 recovery·현재 일정 회귀 보수 | P0 | verifying / stale | contracts/control | 0.5 | — |
| [OPS-003](tasks/OPS-003/task.yaml) | 통합 evidence head race 보정과 과거 binding 호환 | P0 | done / passed | contracts/control | 0.5 | OPS-002 |
| [P0-001](tasks/P0-001/task.yaml) | 빈 저장소 점검, Python 3.12/uv 패키지 구성, 버전 고정 | P0 | done / passed | contracts/control | 0 | — |
| [P0-002](tasks/P0-002/task.yaml) | typed settings, `.env.example`, `.gitignore`, secret-safe doctor와 named dev credential 초기화 | P0 | done / passed | contracts/control | 0 | P0-001 |
| [P0-003](tasks/P0-003/task.yaml) | 공통 Pydantic DTO, async ports, 오류 모델, state transition | P0 | done / passed | contracts/control | 0 | P0-001 |
| [P0-004](tasks/P0-004/task.yaml) | composition root와 adapter 주입 | P0 | done / passed | contracts/control | 0 | P0-002, P0-003 |
| [P0-005](tasks/P0-005/task.yaml) | SQLite 상태, fixture 초기화, checkpoint/KB 책임 분리 | P0 | done / passed | knowledge | 0 | P0-003 |
| [P0-006](tasks/P0-006/task.yaml) | 합성 문서와 deterministic mock/local adapter | P0 | done / passed | knowledge | 0 | P0-003, P0-005 |
| [P0-007](tasks/P0-007/task.yaml) | Supervisor와 공통 Domain TaskGraph, DRAFT, mock 검토 | P0 | done / passed | agents | 0 | P0-004, P0-006 |
| [P0-008](tasks/P0-008/task.yaml) | FastAPI health/readiness/work API와 OpenAPI export | P0 | done / passed | integration/evaluation | 0 | P0-007 |
| [P0-009](tasks/P0-009/task.yaml) | metadata 중심 JSONL trace와 secret redaction | P0 | done / passed | integration/evaluation | 0 | P0-002, P0-007 |
| [P0-010](tasks/P0-010/task.yaml) | loopback provisional HTTP contract와 교체 adapter | P0 | done / passed | integration/evaluation | 0 | P0-003, P0-004 |
| [P0-011](tasks/P0-011/task.yaml) | LLMOps interface와 24개 합성 평가 사례 | P0 | done / passed | integration/evaluation | 0 | P0-003, P0-006 |
| [P0-012](tasks/P0-012/task.yaml) | 개발·통합·교육 문서와 provisional registry | P0 | done / passed | contracts/control | 0 | P0-001, P0-002, P0-003, P0-004, P0-005, P0-006, P0-007, P0-008, P0-009, P0-010, P0-011 |
| [P0-013](tasks/P0-013/task.yaml) | P0 전체 검증과 근거 기록 | P0 | done / passed | contracts/control | 0 | P0-001, P0-002, P0-003, P0-004, P0-005, P0-006, P0-007, P0-008, P0-009, P0-010, P0-011, P0-012 |
| [P0-014](tasks/P0-014/task.yaml) | 식별자·소유권·추적/평가 reference 계약 동결 | P0 | verifying / stale | contracts/control | 1 | P0-003, P0-010 |
| [P0-015](tasks/P0-015/task.yaml) | 사용자 소유 세션·대화·run 조회 | P0 | verifying / stale | execution | 1.5 | P0-014, P0-005, P0-008 |
| [P0-016](tasks/P0-016/task.yaml) | LangGraph SQLite checkpointer·기본 재개 | P0 | verifying / stale | execution | 1.5 | P0-015 |
| [P0-017](tasks/P0-017/task.yaml) | 후속 설정·readiness 기본값 정리 | P0 | verifying / stale | contracts/control | 1 | P0-014, P0-016 |
| [P0-018](tasks/P0-018/task.yaml) | 승인된 팀 template와 규칙 selector | P0 | verifying / stale | agents | 0.5 | P0-014 |
| [P0-019](tasks/P0-019/task.yaml) | Task 전담 팀·TeamFactory·중복 provisioning 방지 | P0 | verifying / stale | agents | 2 | P0-018, P0-015, P0-016 |
| [P0-020](tasks/P0-020/task.yaml) | 역할별 실행·Supervisor 수집·팀 예산·취소 | P0 | done / passed | agents | 3 | P0-019, P0-017, P1-001A, P1-006D |
| [P0-021](tasks/P0-021/task.yaml) | durable 실행 기록·재개 멱등성·결과 대사 | P0 | todo / not_run | execution | 1.5 | P0-016, P0-020, P1-005A |
| [P0-022](tasks/P0-022/task.yaml) | 안전한 예약 DTO·사용자별 일정 관리 | P0 | todo / not_run | execution | 0.5 | P0-014, P0-017, P1-004, P0-021 |
| [P0-023](tasks/P0-023/task.yaml) | APScheduler 3.x 영속 job store·단일 runner | P0 | todo / not_run | execution | 1.5 | P0-022 |
| [P0-024](tasks/P0-024/task.yaml) | 변경 이벤트·누락 실행·안전한 알림 | P0 | todo / not_run | execution | 1 | P0-023, P1-004B, P2-003, P1-005 |
| [P0-025](tasks/P0-025/task.yaml) | UI polling 상태·안전한 이벤트·readiness·registry | P0 | todo / not_run | integration/evaluation | 0.5 | P0-015, P0-020, P0-024, P1-008 |
| [P0-025A](tasks/P0-025A/task.yaml) | 동일 출처 세션·노트·질의·검토 기본 UI | P0 | todo / not_run | integration/evaluation | 2 | P0-015, P1-001A, P1-008C |
| [P0-026](tasks/P0-026/task.yaml) | 합성 end-to-end 데모·D3 전달 패키지 | P0 | todo / not_run | integration/evaluation | 4 | P0-021, P0-024, P0-025, P1-006, P1-008, P1-001B, P1-006E, P1-005B |
| [P0-027](tasks/P0-027/task.yaml) | NAT/LangGraph 호환성 spike·선택 extra 고정 | P0 | done / passed | integration/evaluation | 0.5 | P0-001, P0-007 |
| [P0-028](tasks/P0-028/task.yaml) | NAT 평가 adapter·installed smoke와 native 동등성 | P0 | verifying / stale | integration/evaluation | 1.5 | P0-014, P0-027, P1-006D, P0-016 |
| [P1-001](tasks/P1-001/task.yaml) | 노트·export 입력과 revision 저장 | P0 | verifying / stale | knowledge | 1 | P0-014, P0-015, P0-016 |
| [P1-001A](tasks/P1-001A/task.yaml) | 권한 선필터·최신 lexical 검색·무효화 | P0 | verifying / stale | knowledge | 1.5 | P1-001, P0-016, P1-006 |
| [P1-001B](tasks/P1-001B/task.yaml) | KB L0/L1/L2 선택적 context loader | P0 | todo / not_run | knowledge | 1 | P0-014, P1-001A |
| [P1-001C](tasks/P1-001C/task.yaml) | 계층 요약 최적화·context 로딩 비교(후속) | P1 | deferred / not_run | knowledge | 1 | P1-001B, P1-004A |
| [P1-002](tasks/P1-002/task.yaml) | NVIDIA ModelPort adapter 준비 | P1 | todo / not_run | integration/evaluation | 1.5 | P0-014, P0-017, P1-005 |
| [P1-002A](tasks/P1-002A/task.yaml) | NVIDIA 실제 합성 호출 증거 | P1 | in_progress / passed | integration/evaluation | 0.5 | P0-017 |
| [P1-003](tasks/P1-003/task.yaml) | 공식 NeMo Retriever Skill을 Research 도구에 연결 | P1 | todo / not_run | integration/evaluation | 1 | P0-020, P1-001A, P1-005 |
| [P1-003A](tasks/P1-003A/task.yaml) | 공식 Skill 실제 retrieval 증거 | P1 | verifying / stale | integration/evaluation | 0.5 | P0-017 |
| [P1-004](tasks/P1-004/task.yaml) | 비서 intent 라우팅과 얇은 실행 경계 | P0 | todo / not_run | agents | 1 | P0-016, P1-001A |
| [P1-004A](tasks/P1-004A/task.yaml) | 검토된 지식 축적·provenance | P0 | todo / not_run | knowledge | 0.5 | P0-020, P1-001A |
| [P1-004B](tasks/P1-004B/task.yaml) | TodoCandidate 수락·보류·기각·중복 제거 | P0 | todo / not_run | knowledge | 1 | P1-004A, P0-019 |
| [P1-005](tasks/P1-005/task.yaml) | 대상별 DRAFT·읽기/공유/외부 전송 정책 | P0 | todo / not_run | agents | 1 | P0-020, P1-004A, P1-001B |
| [P1-005A](tasks/P1-005A/task.yaml) | DRAFT 편집·승인 무효화·안전한 mock 게시 상태 | P0 | todo / not_run | agents | 1 | P1-005, P0-016 |
| [P1-005B](tasks/P1-005B/task.yaml) | 피드백 4분류·scope·취소 | P0 | todo / not_run | agents | 0.5 | P1-005A |
| [P1-006](tasks/P1-006/task.yaml) | 핵심 12 Persona QA·행위 verifier·Judge 상태 분리 | P0 | verifying / stale | integration/evaluation | 2 | P0-014, P1-006D |
| [P1-006A](tasks/P1-006A/task.yaml) | 실제 Judge adapter·품질 평가 | P1 | todo / not_run | integration/evaluation | 1.5 | P1-006, P1-002 |
| [P1-006B](tasks/P1-006B/task.yaml) | Persona 시뮬레이션·버전 회귀 비교 | P1 | todo / not_run | integration/evaluation | 1 | P1-006 |
| [P1-006C](tasks/P1-006C/task.yaml) | Langfuse 연결·redaction·보존 확인 | P1 | todo / not_run | integration/evaluation | 1 | P1-006, P1-006D |
| [P1-006D](tasks/P1-006D/task.yaml) | 안전한 trace allowlist·평가 관찰 계약 연결 | P0 | verifying / stale | integration/evaluation | 2 | P0-014, P0-016, P0-017 |
| [P1-006E](tasks/P1-006E/task.yaml) | Workflow 레드팀 4종·정상 대조 회귀 | P0 | todo / not_run | integration/evaluation | 1 | P1-006 |
| [P1-007](tasks/P1-007/task.yaml) | NemoClaw 지원 경로·교육 연결 설계 | P1 | todo / not_run | integration/evaluation | 0.5 | P0-014, P0-025 |
| [P1-007A](tasks/P1-007A/task.yaml) | NemoClaw 운영 경로 실제 시연 | P1 | blocked / not_run | integration/evaluation | 0.5 | P1-007, P1-008B |
| [P1-007B](tasks/P1-007B/task.yaml) | OpenShell 역할별 허용·차단 증거 | P1 | blocked / not_run | integration/evaluation | 0.5 | P1-008B |
| [P1-008](tasks/P1-008/task.yaml) | Response/Tool/Runtime/Policy reference 계약 완성 | P0 | todo / not_run | integration/evaluation | 1 | P0-014, P0-020, P0-021, P1-005A, P1-006D |
| [P1-008A](tasks/P1-008A/task.yaml) | 승희 Response/Tool 실제 교체 | P1 | blocked / not_run | integration/evaluation | 1 | P1-008, P0-026 |
| [P1-008B](tasks/P1-008B/task.yaml) | 다영 Runtime·UI 실제 교체 | P1 | blocked / not_run | integration/evaluation | 1 | P1-008, P0-025, P0-026 |
| [P1-008C](tasks/P1-008C/task.yaml) | 수동 승인·모의 게시·READ tool 로컬 대체 모듈 | P0 | todo / not_run | integration/evaluation | 2 | P0-014 |
| [P1-008D](tasks/P1-008D/task.yaml) | 합성 handler와 영속 lifecycle 로컬 runtime 대체 모듈 | P0 | todo / not_run | integration/evaluation | 2 | P0-014, P1-008C |
| [P1-009](tasks/P1-009/task.yaml) | D4 회귀·교육/심사 증거·최종 상태 확정 | P1 | todo / not_run | integration/evaluation | 1 | P0-026 |
| [P2-001](tasks/P2-001/task.yaml) | clustering 기반 Task 후보 제안 | P2 | deferred / not_run | knowledge | 1.5 | P1-004B, P2-003 |
| [P2-002](tasks/P2-002/task.yaml) | 제한된 DebateLease | P2 | deferred / not_run | agents | 2 | P2-003, P1-006B, P0-020 |
| [P2-003](tasks/P2-003/task.yaml) | 설명 가능한 후보 기본 정렬 | P0 | todo / not_run | knowledge | 0.5 | P1-004B |
| [P2-004](tasks/P2-004/task.yaml) | 추가 connector 한 개의 read-only slice | P2 | deferred / not_run | knowledge | 1.5 | P1-001A, P1-008A |
| [P2-005](tasks/P2-005/task.yaml) | 실제 GPU benchmark 최소 실행 | P2 | deferred / not_run | integration/evaluation | 2 | P0-020, P1-007B |
| [P2-006](tasks/P2-006/task.yaml) | Engineering template 실행 slice | P2 | deferred / not_run | agents | 2 | P0-020, P1-007B, P1-008A |
| [P2-007](tasks/P2-007/task.yaml) | 팀 재구성·template 버전 전환 | P2 | deferred / not_run | agents | 1 | P0-019, P0-021 |
| [P2-008](tasks/P2-008/task.yaml) | OpenClaw ChannelAdapter 최소 연결 | P2 | deferred / not_run | integration/evaluation | 1 | P1-008A, P0-015 |
| [P2-009](tasks/P2-009/task.yaml) | 실행 가능한 후보 내 LLM 팀 선택 | P2 | deferred / not_run | agents | 1 | P0-018, P1-002A, P1-006B |

## 다음 작업


## 최종 gate

- local_product: P0-026=not_passed
- nat_installed_fake_provider: P0-028=not_passed
- real_technology: P1-002A=not_passed, P1-003A=not_passed, P1-007A=not_passed, P1-007B=not_passed
- teammate_modules: P1-008A=not_passed, P1-008B=not_passed
- final_reporting: P1-009=not_passed
- control_migration: OPS-001=not_passed
