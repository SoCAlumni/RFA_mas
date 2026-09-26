# TASKS → per-task YAML 이관 기록

개발 시작 갱신(2026-09-26): 사용자 승인으로 기존 파일·E2E 원문을 보존한 첫 커밋 `d51137a`를 생성했고 canonical `main` baseline을 등록했다. 아래 Git 미등록 및 제품 구현 미승인 표현은 이관 당시 기록이다. 이후 진행·실패·커밋은 [작업 기록](WORK_LOG.md)과 task별 원본/evidence를 따른다.

후속 명세 보강(2026-09-26): 아래는 최초 이관 당시 기록이다. 현재 Git은 초기화됐으나 HEAD가 없어 OPS-000 baseline은 계속 미등록이다. 기술 고도화로 기존 ID는 유지하고 6개 task를 추가했으며 [대응·공수·의존성 변경](EVALUATION_CONTEXT.md)을 따른다. 추가 P0 5개는 4.5h, 후속 P1 1개는 1h다. 최초 완료 기반/24개 fixture/원문 해시는 변경하지 않았다. 새 AC는 제품 not_run이며 최초 60개·24h 수치는 역사 기록으로 보존한다.

## 조사와 원본 보존

2026-09-26 KST. AGENTS.md, README, 기존 TASKS, pyproject/uv.lock, 설정 코드, graph/DTO/ports/adapters/SQLite/mock/tests 및 기존 통합·교육 문서를 조사했다. 실제 `.env`·token·사용자 DB/trace 본문을 읽지 않았다. CLAUDE.md, tasks.yaml, Spec Kit/GSD/Beads 등 기존 상태 CLI는 발견되지 않았다. RFA bootstrap/scenario 두 문서는 기존 조사에서도 없었으며 내용을 추정하지 않는다.

기존 TASKS 전체(58개 task, V-001~V-012, 모든 AC·주석·링크)는 [원문](archive/TASKS_2026-09-26.md)에 byte 내용 그대로 보존했다. SHA-256: `73d1f72461a6c3696c101be6418fc7a8782bdf8c2c7cfd0648f533133a36e6b0`. archive는 역사 증거이며 현재 상태 원본이 아니다. 각 task.yaml의 migration은 당시 완료 주장/구현 사실/과거 evidence/이번 점검을 분리한다. 공통 요구사항은 PRODUCT_REQUIREMENTS에 옮겼고 세부 요구사항을 global 요약으로 대체하지 않았다.

현재 root/상위에 `.git`이 없어 git status/diff는 저장소 관점에서 불가능하다. tracked·dirty·통합 commit을 꾸며내지 않는다. 이번 새 파일은 기존 파일을 보존하며 추가했고 archive 대조 및 `git diff --no-index --check` 방식으로 확인한다. OPS-000은 사용자의 기존 Git checkout 또는 baseline 생성 방침을 기다린다. 기능 claim은 source baseline 미등록으로 차단되지만 이관/계약 조사/임시 fixture 검증은 계속했다.

## 원본/파생과 책임

- 원본: `tasks/project.yaml`, `tasks/<ID>/task.yaml`; 짧은 global `docs/PROJECT_CONTEXT.md`; 현재 계약은 Pydantic.
- 요약: task별 handoff(완료 이력과 최초 재개 P0-014, OPS 작업만 실제 내용으로 작성). 나머지는 `docs/templates/HANDOFF.md`로 필요 시 생성; 가짜 완료 handoff를 만들지 않았다.
- 파생: `tasks/index.yaml`, `tasks/state.yaml`, `TASKS.md`; `taskctl refresh`만 갱신. 각 source revision/digest와 생성 시각 포함, 변화 없으면 byte-identical.
- 계약 파생: `scripts/contract_baseline.py` → `docs/contracts/baseline.json`, 합성 정상/거절/부족/부분실패/timeout fixture. 이 파일은 기존 schema 1.0이며 새 확장 계약 P0-014나 상대 실제 호환성을 대신하지 않는다.
- 공유 control root: `/Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas`. feature checkout의 사본을 상태 원본으로 쓰지 않는다. 공통 경로 없는 원격 세션은 coordinator 직렬 명령으로만 운영한다.

## 기존→신규 대응

모든 ID 유지. 기존 13 done / 31 todo / 6 blocked / 8 deferred를 보존한다. OPS-000/001 두 개는 제품 범위를 늘리지 않는 운영 task다. 과거 done은 구현과 기존 offline 검증을 보존한 제한된 working-copy 완료이며 Git 통합 완료나 확장 P0 완료가 아니다.

| 기존 ID | 신규 원본 | 원래 상태 → 이관 상태 | 판단 |
| --- | --- | --- | --- |
| P0-001 | [task.yaml](../tasks/P0-001/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-002 | [task.yaml](../tasks/P0-002/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-003 | [task.yaml](../tasks/P0-003/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-004 | [task.yaml](../tasks/P0-004/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-005 | [task.yaml](../tasks/P0-005/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-006 | [task.yaml](../tasks/P0-006/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-007 | [task.yaml](../tasks/P0-007/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-008 | [task.yaml](../tasks/P0-008/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-009 | [task.yaml](../tasks/P0-009/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-010 | [task.yaml](../tasks/P0-010/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-011 | [task.yaml](../tasks/P0-011/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-012 | [task.yaml](../tasks/P0-012/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-013 | [task.yaml](../tasks/P0-013/task.yaml) | done → done | 기반 코드/과거 검증 유지 |
| P0-014 | [task.yaml](../tasks/P0-014/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-015 | [task.yaml](../tasks/P0-015/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-016 | [task.yaml](../tasks/P0-016/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-017 | [task.yaml](../tasks/P0-017/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-018 | [task.yaml](../tasks/P0-018/task.yaml) | todo → todo | 순수 selector 파일 격리·router 선행 제거; AC 유지 |
| P0-019 | [task.yaml](../tasks/P0-019/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-020 | [task.yaml](../tasks/P0-020/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-021 | [task.yaml](../tasks/P0-021/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-022 | [task.yaml](../tasks/P0-022/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-023 | [task.yaml](../tasks/P0-023/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-024 | [task.yaml](../tasks/P0-024/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-025 | [task.yaml](../tasks/P0-025/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P0-026 | [task.yaml](../tasks/P0-026/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-001 | [task.yaml](../tasks/P1-001/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-001A | [task.yaml](../tasks/P1-001A/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-002 | [task.yaml](../tasks/P1-002/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-002A | [task.yaml](../tasks/P1-002A/task.yaml) | blocked → blocked | 실제 provider/팀원 환경만 blocked |
| P1-003 | [task.yaml](../tasks/P1-003/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-003A | [task.yaml](../tasks/P1-003A/task.yaml) | blocked → blocked | 실제 provider/팀원 환경만 blocked |
| P1-004 | [task.yaml](../tasks/P1-004/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-004A | [task.yaml](../tasks/P1-004A/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-004B | [task.yaml](../tasks/P1-004B/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-005 | [task.yaml](../tasks/P1-005/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-005A | [task.yaml](../tasks/P1-005A/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-005B | [task.yaml](../tasks/P1-005B/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-006 | [task.yaml](../tasks/P1-006/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-006A | [task.yaml](../tasks/P1-006A/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-006B | [task.yaml](../tasks/P1-006B/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-006C | [task.yaml](../tasks/P1-006C/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-007 | [task.yaml](../tasks/P1-007/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-007A | [task.yaml](../tasks/P1-007A/task.yaml) | blocked → blocked | 실제 provider/팀원 환경만 blocked |
| P1-007B | [task.yaml](../tasks/P1-007B/task.yaml) | blocked → blocked | 실제 provider/팀원 환경만 blocked |
| P1-008 | [task.yaml](../tasks/P1-008/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P1-008A | [task.yaml](../tasks/P1-008A/task.yaml) | blocked → blocked | 실제 provider/팀원 환경만 blocked |
| P1-008B | [task.yaml](../tasks/P1-008B/task.yaml) | blocked → blocked | 실제 provider/팀원 환경만 blocked |
| P1-009 | [task.yaml](../tasks/P1-009/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P2-001 | [task.yaml](../tasks/P2-001/task.yaml) | deferred → deferred | 4일 밖 후순위 유지 |
| P2-002 | [task.yaml](../tasks/P2-002/task.yaml) | deferred → deferred | 4일 밖 후순위 유지 |
| P2-003 | [task.yaml](../tasks/P2-003/task.yaml) | todo → todo | 미구현 todo; 실제 검증 not_run |
| P2-004 | [task.yaml](../tasks/P2-004/task.yaml) | deferred → deferred | 4일 밖 후순위 유지 |
| P2-005 | [task.yaml](../tasks/P2-005/task.yaml) | deferred → deferred | 4일 밖 후순위 유지 |
| P2-006 | [task.yaml](../tasks/P2-006/task.yaml) | deferred → deferred | 4일 밖 후순위 유지 |
| P2-007 | [task.yaml](../tasks/P2-007/task.yaml) | deferred → deferred | 4일 밖 후순위 유지 |
| P2-008 | [task.yaml](../tasks/P2-008/task.yaml) | deferred → deferred | 4일 밖 후순위 유지 |
| P2-009 | [task.yaml](../tasks/P2-009/task.yaml) | deferred → deferred | 4일 밖 후순위 유지 |

상위 D1/D2/D3/D4/LLMOps/P2 묶음은 project.milestones로 집계하며 claim하지 않는다. 완료 하위 묶음을 근거로 최종 제품 gate를 자동 done으로 만들지 않는다.

## 판단/변경 이유

- 기존 P1-001/004/005/006/008, P2-003의 P0 승격과 모든 split/deferred 연결은 PRODUCT_REQUIREMENTS의 기존 ID 이력 표에 보존했다. 이번 이관에서 우선순위를 다시 바꾸지 않았다.
- P0-018만 의존성과 파일 경계를 좁혔다. 순수 TeamSelector는 P0-014 계약 + 합성 TeamTemplate만 필요하므로 P1-004 라우터 구현 의존을 제거했다. 신규 `application/team_selector.py`, `tests/test_team_selector.py`에 격리하고 router 연결은 P0-020, DTO는 P0-014로 남긴다. 같은 기능의 공백을 삭제하지 않는다.
- 기존 카드에서 TASKS.md를 수정하던 산출물은 generated view 갱신으로 전환했다. source의 증거/상태만 변경하며 worker가 TASKS를 commit하지 않는다.
- 확장 DTO를 실제로 소비하는 23개 작업과 상대 실제 스키마 미확인 6개 작업은 spec=draft다. 목표/AC/계획은 상세하지만 확정되지 않은 계약 digest를 placeholder로 ready 처리하지 않는다. P0-014의 schema/fixture 후 coordinator가 영향 spec을 검토해 ready로 올린다. 다른 todo의 계약 준비 여부와 실행 의존성을 분리한다.
- 새 상태 verifying은 worker 검증/통합 대기 또는 stale 재검증이며 기존 task들을 일괄 todo로 되돌리지 않았다. prior done 예외는 archive SHA와 원래 done ID로만 제한한다.

## 다음 순서 / 병렬과 외부 의존

OPS-000의 source baseline 확인이 먼저다. 제품의 첫 3개 순차 task는 P0-014(식별자·권한/소유권 DTO와 fixture/OpenAPI), P0-015(두 사용자 세션 조회·migration·재시작), P0-016(SQLite checkpointer interrupt/restart/resume)이다. 첫 계약 이후 P0-018 selector를 별도 worker로 함께 개발할 수 있다. P0-015와 KB 작업의 local.py/migration은 충돌하므로 직렬이다. 등록된 source baseline이 없으면 실제 병렬 claim을 허용하지 않는다.

D1~D3 8h씩 총 24h, D4 조건부 8h는 그대로다. 민섭 실제 LLMOps 후속 3.5h와 P2는 별도다. 승희 승인/게시 서비스, 다영 Runtime/UI, NVIDIA key/지원 모델, Retriever 실행 환경, NemoClaw 지원 런타임, OpenShell 실제 권한 경계는 외부 준비가 필요하다. 최신 registry 실스키마·기밀 검수 최종 소유권도 미확정이다. 준비된 mock/reference 계약 작업은 이와 무관하게 진행한다.

## 이관 검증 / 제품 검증의 구분

- 운영 제어 검증은 `tests/test_taskctl.py`, 원문 대응은 `tests/test_task_migration.py`. 임시 control/두 Git worktree에서 경쟁·fencing·source/view 실패·증거·통합 gate를 시험한다. 실제 제품 task claim에 합성 성공을 쓰지 않는다.
- `tests/test_contract_baseline.py`는 기존 schema/정상·거절·부족·부분 실패·timeout fixture와 local/reference 실제 응답을 검사한다. NVIDIA/팀원 서비스 검증 아님.
- 검증 도중 디스크 공간 부족으로 임시 fixture 생성이 실패한 실행은 환경 오류이며 passed에 포함하지 않는다. 이 테스트가 만든 pytest-2/pytest-3 임시 데이터만 확인 후 정리했고 실제 사용자 파일/제품 데이터를 삭제하지 않았다.
- 최종 실행 명령·실제 collected/passed/skip 수·시각·source manifest는 OPS-001의 불변 evidence에 기록한다. 생성된 TASKS/status에서 이관 audit 결과를 조회한다. 과거 170 offline tests 결과와 이번 실행은 혼합하지 않는다.
- 확장 제품 P0-026 E2E, 지속 세션/예약/팀/승인 복구 및 P1 actual NVIDIA/Skill/NemoClaw/OpenShell/팀원 실연동은 **not_run**이다. 이번 요청으로 제품 전체를 구현하거나 실제 게시하지 않았다.

## 운영 한계

CLI는 같은 호스트의 trusted 개발 세션을 대상으로 한다. 수동 report를 형식/AC/source와 결합하지만 테스트를 몰래 실행하거나 작성자의 attestation 진실성을 인증하는 서비스는 아니다. 별도 세션·worktree 생성, heartbeat, 프로세스 중지/부작용 대사, source commit/merge와 충돌 해결, 실제 UI 관찰은 명시적인 수동 작업이다. 상태 lock/atomic replace/fencing/증거 검사는 구현했다. OpenAI Docs 스킬로 공식 새 세션/worktree 동작을 확인해 복사형 프롬프트에 반영했으며 context pack만으로 context가 초기화되었다고 설명하지 않는다.
