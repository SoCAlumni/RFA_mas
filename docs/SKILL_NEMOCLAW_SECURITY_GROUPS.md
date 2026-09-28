# NemoClaw Security Groups Skill

기준: main, 2026-09-28. 주장 범위는 [JUDGING_KEYWORDS.md](JUDGING_KEYWORDS.md)를 따른다.

## 한 줄

OpenShell 네트워크 정책 preset을 "보안 그룹"으로 추상화하고, 코딩 에이전트가 이를 선언·미리보기·적용·검증하도록 절차화한 NemoClaw 운영 스킬이다. 이 프로젝트는 NVIDIA 스킬을 **쓰기도 하고 만들기도** 했다.

## 왜 필요했나

NemoClaw는 정책을 샌드박스 단위로만 붙인다. 에이전트가 늘면 "이 에이전트 그룹이 어디로 나갈 수 있나"를 한눈에 보고 한 번에 바꿀 방법이 없다. 그래서 EC2 보안 그룹처럼 preset에 이름을 붙여 재사용하고, 샌드박스를 그룹의 조합으로 선언하고, 선언과 live 상태의 차이를 reconcile하는 층을 만들었다. 그 운영 절차를 스킬로 굳혔다. NemoClaw 기본 기능에는 없는 추상화다.

## 구성

```
deploy/nemoclaw/skills/nemoclaw-security-groups/
  SKILL.md          작업별 명령 표와 운영 규칙
  scripts/sg.sh     저장소 루트에서 컨트롤러를 실행하는 래퍼
deploy/nemoclaw/assignments.yaml     보안 그룹 정의, 샌드박스 → 그룹, 에이전트 → 그룹
deploy/nemoclaw/presets/sg-*.yaml    그룹이 쓰는 preset (OpenShell preset 형식 그대로)
deploy/nemoclaw/baseline/            생성 시 고정되는 static 정책 선언
src/rfa_mas/nemoclaw/controller.py   선언을 읽어 plan/apply로 reconcile
src/rfa_mas/nemoclaw/requests.py     차단된 요청의 수집·승인·거절
```

보안 그룹은 세 개다.

| 그룹 | privilege | 여는 경로 |
| --- | --- | --- |
| `egress-none` | 0 | 없음. managed inference만 |
| `intranet-ro` | 1 | preset `sg-intranet-ro`: 사내 지식 API `GET /healthz`, `GET /tasks`, `POST /tasks/*/ask` |
| `control-plane` | 2 | 브로커 managed MCP. 등록이 안 될 때만 preset `sg-control-plane`(REST)로 폴백 |

## 스킬이 시키는 절차

| 단계 | 명령 |
| --- | --- |
| 선언 검사 | `scripts/sg.sh validate` |
| 바뀔 내용 미리보기 | `scripts/sg.sh plan` |
| live 샌드박스에 반영 | `scripts/sg.sh apply` |
| 샌드박스별 live 상태 | `scripts/sg.sh status` |
| static 정책이 선언과 같은지 | `scripts/sg.sh verify-baseline` |
| 에이전트가 읽는 정책 설명 갱신 | `scripts/sg.sh explain` |
| 에이전트를 다른 그룹으로 이동 | `scripts/sg.sh relocate <agent> --to-groups <g1,g2> [--wipe]` |
| 차단된 요청 처리 | `scripts/sg.sh requests sync`, `list`, `approve <id> --reason "..."`, `deny <id>` |
| 추론 경로 전환 | `scripts/sg.sh switch-route rfa-internal` / `rfa-auto` |

## 안전장치

- 컨트롤러는 NemoClaw CLI 동사만 쓴다: `policy add --from-file`, `policy remove`, `policy exclude`, `agents apply`, `mcp add`, `policy explain --write`. `openshell policy set`은 호출하지 않는다. rebuild 때 정책이 보존되게 하기 위해서다.
- 적용 전에 `plan`으로 실행될 명령을 먼저 본다. 적용 뒤 다시 `plan`을 돌려 변경 없음을 확인한다.
- 에이전트의 `groups`가 배치된 샌드박스 groups의 부분집합이 아니면 `validate`가 실패한다.
- 모든 샌드박스에서 baseline의 외부 경로 5개(`nvidia`, `clawhub`, `openclaw_api`, `openclaw_docs`, `npm_registry`)를 `policy exclude`한다.
- 되돌리기는 선언을 되돌린 뒤 `apply`하는 방식이다. 컨트롤러가 `policy remove`를 실행한다.
- 차단된 요청을 승인하면 그 host, port, method, path, 바이너리에만 해당하는 preset `sg-approved-<id>`가 만들어진다. 승인에는 사유가 필요하다.
- 낮은 권한 그룹으로 옮길 때는 workspace를 external 검열 프로파일로 스캔해 redacted 사본만 옮긴다. 막힌 파일이 있으면 이동을 중단한다.

## 평가 기준 대응

| 기준 | 이 스킬이 주는 것 |
| --- | --- |
| 1. NVIDIA 기술 활용 심도 | NemoClaw preset, policy CLI, rebuild 보존 규칙을 실제로 쓰고 SKILL.md 형식의 운영 스킬을 냈다. 개발 중에는 공식 NemoClaw 사용자 스킬(`nemoclaw-user-*`)을 설치해 사용했고, 코어층에서는 공식 `nemo-retriever` 스킬(CLI 26.8.1)을 사용했다 |
| 2. 실용성·산업가치 | 보안팀이 승인할 수 있는 형태로 에이전트 여러 개를 운영하는 데 빠진 조각이다. 그룹 하나를 고치면 그 그룹을 쓰는 샌드박스에 반영된다 |
| 3. 완성도 | 컨트롤러 단위 테스트(`tests/test_sg_controller.py`, `tests/test_sg_ops.py`)가 있다. 라이브에서 preset drift 감지 → `apply` 8.3초 → 재`plan` 변경 없음을 확인했다(WORK_LOG SG-8, n=1) |
| 4. 커스터마이징·독창성 | 플랫폼이 제공하지 않는 추상화를 플랫폼 부품만으로 구현했다 |

## 데모

`demo/02_security_group_change.py`: `assignments.yaml`의 `control-plane` 그룹에 preset `sg-control-plane`을 추가 → `plan` → `apply`(`policy add`) → 샌드박스에서 브로커 `/healthz` 200 → 선언을 되돌리고 `apply`(`policy remove`) → 다시 차단.

## 아직 없는 것

| 항목 | 상태 |
| --- | --- |
| `references/` (preset 스키마, 그룹 카탈로그, 롤백 절차) | 없음. 같은 내용이 `assignments.yaml`과 `presets/` 주석에 흩어져 있다 |
| `evals/evals.json` | 없음. 스킬 자체의 eval은 없고 컨트롤러 단위 테스트만 있다 |
| 적용 전 `policy get` 자동 백업과 자동 롤백 | 없음. 되돌리기는 선언을 되돌려 `apply`하는 방식이다 |
| 여러 샌드박스 동시 적용 기록 | 없음. 기본 운영 샌드박스는 `rfa-main` 1개다 |
| 단독 설치 | 스킬은 이 저장소의 컨트롤러(`python -m rfa_mas.nemoclaw`)에 의존한다. 저장소 밖에서는 동작하지 않는다 |
| `demo/replay/` 라이브 기록 | 없음 |

## 증거

| 항목 | 위치 |
| --- | --- |
| 스킬 | `deploy/nemoclaw/skills/nemoclaw-security-groups/SKILL.md`, `scripts/sg.sh` |
| 선언과 preset | `deploy/nemoclaw/assignments.yaml`, `deploy/nemoclaw/presets/sg-*.yaml` |
| 컨트롤러 | `src/rfa_mas/nemoclaw/controller.py`, `requests.py`, `relocate.py` |
| 테스트 | `tests/test_sg_controller.py`, `tests/test_sg_ops.py` |
| 데모 | `demo/02_security_group_change.py`, `demo/01_external_curl_blocked.py`, `demo/08_demote_workspace_scan.py` |
| 라이브 기록 | [WORK_LOG.md](WORK_LOG.md) SG-8 |
| 공식 `nemo-retriever` 스킬 사용 증거 | [evidence/nvidia-skill.md](evidence/nvidia-skill.md), [evidence/nvidia-skill-product.md](evidence/nvidia-skill-product.md) |
| 공식 NemoClaw 사용자 스킬 사용 | 팀원 개발 환경에 설치해 사용했다. 이 저장소에는 설치 기록이 없다 |
