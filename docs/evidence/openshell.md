# OpenShell 역할별 허용·차단 증거 (E2E-05, P1-007C)

**범위 표시: real OpenShell, local standalone. teammate runtime/identity(P1-008B)와 NemoClaw(P1-007A)는 포함하지 않는다.**

이 문서는 이 Mac에서 공식 OpenShell v0.1.1 gateway(VM compute driver)를 실제로 실행하고, E2E-05의 역할별 파일·네트워크·실행 허용과 차단을 합성 자료만으로 재현한 결과다. 역할 정책은 stand-in이다(조사 worker = paper_scout, 실행 worker = experiment_runner). RFA 제품의 RuntimePort가 OpenShell에서 역할을 실행하는 경로는 아직 없으므로 이 결과는 제품 통합 보안 gate의 통과가 아니다. Supervisor 메시지 경계는 OpenShell과 별도로 애플리케이션 코드에서 측정했다.

## 1. 결과 요약

증거 실행 run_id는 `0926181417`이다. 시각은 2026-09-26 18:14:17–18:16:16 UTC(2026-09-27 03:14–03:16 KST), 소스는 commit `83fccfb`이다. 실행 명령은 opt-in live test이며 결과는 **5 passed (118.93 s)**였다. 모든 matrix 행이 기대 분류와 일치했다(status `passed`). 역할마다 새 sandbox를 3번 만들었고, 허용 probe는 반복 5회, 차단 probe는 반복 3회였다. 자동 재시도는 0회다.

| step | 역할 | probe | 기대 | 관측 결과 | 분류 (n) | OpenShell 증거 |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | 조사 | 허용 host:port의 `GET /public/R01…` (python3.12) | allowed | 200, 본문 sha256 = host 원본 | allowed (15) | `HTTP:GET [INFO] ALLOWED GET http://192.168.123.191:52834/public/R01_release_notice_v1.txt [policy:rfa_public_docs engine:l7]` |
| R2 | 조사 | 같은 host:port의 `GET /internal/R02…` (정책에 없는 path) | denied_by_policy | 403 `{"error":"policy_denied","layer":"l7"}`, upstream 도달 0건 | denied_by_policy (9) | `HTTP:GET [MED] DENIED GET http://192.168.123.191:52834/internal/R02_roadmap.txt [policy:rfa_public_docs engine:l7] [reason:L7_REQUEST deny …]` |
| R3 | 조사 | 정책에 없는 두 번째 host port | denied_by_policy | `URLError errno 13`, 비허용 서버 도달 0건 | denied_by_policy (9) | `NET:OPEN [MED] DENIED /usr/local/bin/python3.12(0) -> 192.168.123.191:52835 [reason:transparent_tcp_policy_denied]` |
| R4 | 조사 | 미승인 binary(bash를 exec로 직접 실행)가 허용 endpoint 접속 | denied_by_policy | `connect: Permission denied` | denied_by_policy (9) | `NET:OPEN [MED] DENIED /usr/bin/bash(0) -> 192.168.123.191:52834 [reason:transparent_tcp_policy_denied]` |
| R4b | 조사 | 승인된 python3.12가 띄운 bash가 허용 endpoint 접속 (정보) | allowed | 200 | allowed (3) | `NET:OPEN [INFO] ALLOWED /usr/bin/bash(0) -> 192.168.123.191:52834 [policy:rfa_public_docs engine:opa]` |
| R5 | 조사 | host 출처 sentinel `/srv/rfa-private/SENTINEL_E2E05.txt` 읽기 | denied_by_policy | `PermissionError errno 13` | denied_by_policy (9) | policy differential(§3): 통제 sandbox는 같은 파일을 읽음(sha256 = host, mode 0644, uid 0) |
| R6 | 조사 | 같은 sentinel을 host 절대 경로로 읽기 | inconclusive | `FileNotFoundError errno 2` | inconclusive (3) | microVM은 host 파일시스템을 mount하지 않는다. 부재는 정책 거절로 세지 않는다 |
| R7 | 조사 | 거절 후 비허용 endpoint 재시도 | denied_by_policy | errno 13. advisor 제안 pending 2, approved 0 (약 7.5 s 대기) | denied_by_policy (3) | `NET:OPEN [MED] DENIED /usr/local/bin/python3.12(0) -> 192.168.123.191:52835 [reason:transparent_tcp_policy_denied]` |
| E1 | 실행 | `/sandbox/workdir`에서 승인된 CPU 계산(R03/R04 metric compare) | allowed | `latency_delta_ms -1.8, latency_change_pct -18.0, accuracy_delta_pts -0.2`, result.json 작성 | allowed (15) | 계산값과 결과 파일로 판정. network·파일 거절 없음 |
| E2 | 실행 | `/tmp` 쓰기 (1777, 승인 workdir 밖) | denied_by_policy | `PermissionError errno 13` | denied_by_policy (9) | policy differential: 통제 sandbox는 같은 경로에 씀 |
| E3 | 실행 | `/opt/rfa-unapproved/unapproved_tool.sh` 실행 (0755, 승인 경로 밖) | denied_by_policy | `PermissionError errno 13`, marker 미출력 | denied_by_policy (9) | policy differential: 통제 sandbox는 같은 tool을 실행하고 marker 출력 |
| E4 | 실행 | 실행 역할의 network egress (network rule 없음) | denied_by_policy | `URLError errno 13` | denied_by_policy (9) | `NET:OPEN [MED] DENIED /usr/local/bin/python3.12(0) -> 192.168.123.191:52834 [reason:transparent_tcp_policy_denied]` |
| E5 | 실행 | `/etc` 쓰기 (root 소유 0755) | inconclusive | `PermissionError errno 13` | inconclusive (3) | Unix 권한만으로도 설명되어 정책 거절로 판정하지 않음 |
| E6 | 실행 | 쓰기 가능한 workdir에 복사한 binary 실행 (정보) | allowed | rc 0 | allowed (3) | OpenShell 0.1.1 경로 정책은 쓰기 가능 경로 안의 실행을 막지 않는다 |

거절 행에는 비서가 받을 안전한 사유 코드가 있다. `policy_denied.request`(R2), `policy_denied.network`(R3·R7·E4), `policy_denied.binary_identity`(R4), `policy_denied.filesystem`(R5·E2), `policy_denied.execution`(E3)다. 증거 JSON에는 파일·응답 본문이 없고 sha256·크기·errno만 있다. 보고서 전체에 sentinel 표식 문자열이 없음을 자동으로 검사했다(`canary_absent_from_report: true`). 이 코드는 orchestrator가 부여한 것이며, 제품 비서까지 전달하는 경로는 아직 없다.

## 2. 판정 규칙

- **allowed**: 작업이 성공했다. network 허용은 OpenShell `ALLOWED` 기록도 있어야 한다.
- **denied_by_policy**: network는 실패와 같은 시간대의 `DENIED` OCSF 기록이 함께 있고, 두 endpoint가 probe 전후 host health check에서 모두 200이어야 한다. 파일·실행은 errno 13이고 **policy differential**이 성립해야 한다.
- **inconclusive**: 파일 부재(errno 2), endpoint 다운, 정책 기록 없는 일반 오류, Unix 권한만으로도 설명되는 실패다.

OpenShell 0.1.1의 Landlock 파일 거절은 접근별 감사 event를 남기지 않는다. 문서상 filesystem 로그는 시작 시 ruleset 관련 `CONFIG` event뿐이다. 그래서 파일·실행 거절은 차이가 해당 경로 하나뿐인 통제 정책의 sandbox에서 같은 파일과 같은 작업이 성공하는지로 귀속시켰다. 대상은 모두 Unix 권한상 sandbox 사용자에게 허용된 것이다(sentinel 0644, /tmp 1777, tool 0755). 이 규칙은 `tests/integration/test_openshell_live.py`의 기본 테스트가 고정한다. 탐색 단계에서 합성 HTTP 서버가 내려가 있을 때 OpenShell은 `NET:FAIL [LOW]`만 남겼고, 이 판정 규칙은 그것을 거절로 세지 않는다.

## 3. Sandbox·정책 식별자

| sandbox | 역할 | sandbox ID | policy version | policy hash |
| --- | --- | --- | --- | --- |
| `e5-181417-rc` | 조사 통제(/srv/rfa-private 읽기 허용) | `2ef51657-f4af-4f0a-942e-48c749bd06ff` | 2 | `82ef4dd3ce3d7615…` |
| `e5-181417-xc` | 실행 통제(/tmp 쓰기, /opt/rfa-unapproved 허용) | `9e7af3e5-0877-4d5a-9915-53a417673768` | 1 | `c5daa0286e17c386…` |
| `e5-181417-r1` / `r2` / `r3` | 조사 | `9a1caf3e-f2ed-4fd6-85b7-7773bfbc1b7d` / `c47d0db0-146b-4dfe-8a64-67ad6195f1a0` / `4b614571-3296-400c-b1de-397fe4e1797e` | 2 | `ae60e8bb27d157be2c517652566cfea759249626f1a1180b097475dc6a908a82` |
| `e5-181417-x1` / `x2` / `x3` | 실행 | `6d8c3aaf-d9c5-4373-b684-e247fc2ddd50` / `41361519-2f3b-47d4-9970-421efd3c3e46` / `951b4657-60e5-473e-a062-9af6eec3c5ff` | 1 | `bd093921c54f5241bbea189dae8b8bf6c7cc7e74f8b0dbbecd1dd6e4b19e8e1c` |

조사 정책이 version 2인 것은 OpenShell이 network rule이 있는 정책에 baseline 경로(`/var/log` 등)를 더해 저장하기 때문이다(`CONFIG:ENRICHED`). 두 역할 정책 모두 `landlock.compatibility: hard_requirement`이다. 따라서 Landlock을 적용하지 못하면 sandbox가 시작되지 않는다. 실행 역할 정책에는 network rule이 없어 baseline 경로가 추가되지 않았고, 쓰기 가능한 경로는 `/sandbox`와 `/dev/null`뿐이다. 렌더링된 정책 파일의 sha256은 보고서 `environment.policies_sha256`에 있다.

## 4. 지연 (ms)

| 측정 | n | median | min | max |
| --- | ---: | ---: | ---: | ---: |
| cold 준비: 조사 sandbox 생성(Ready까지) | 3 | 7070.5 | 6951.4 | 8735.8 |
| cold 준비: 실행 sandbox 생성 + workdir upload | 3 | 7361.3 | 7046.2 | 7750.4 |
| └ workdir upload | 3 | 105.8 | 88.7 | 170.0 |
| warm 허용 읽기: exec 왕복 | 15 | 190.7 | 177.7 | 282.2 |
| warm 허용 읽기: sandbox 안 요청 시간 | 15 | 9.491 | 8.321 | 22.263 |
| 기준: host에서 같은 URL 직접 요청 | 15 | 0.508 | 0.357 | 3.448 |
| warm 승인 계산: exec 왕복 | 15 | 236.0 | 214.3 | 476.4 |
| 거절: L7 path (R2, sandbox 안) | 9 | 9.162 | 8.443 | 13.338 |
| 거절: L4 port (R3) | 9 | 7.884 | 7.259 | 8.594 |
| 거절: binary identity (R4) | 9 | 1.109 | 0.699 | 4.200 |
| 거절: 파일 읽기 (R5) | 9 | 0.019 | 0.011 | 0.603 |
| 거절: 파일 쓰기 (E2) | 9 | 0.021 | 0.012 | 0.045 |
| 거절: 실행 (E3) | 9 | 0.333 | 0.191 | 1.133 |
| 거절: 실행 exec 왕복 (E3) | 9 | 176.9 | 166.6 | 192.2 |

허용 요청의 sandbox 안 median은 9.5 ms로 host 직접 요청(0.5 ms)보다 약 9 ms 길다. 이 차이는 VM 경계의 transparent 중계와 L7 검사를 포함한 값이다. exec 왕복(약 0.2 s)은 CLI→gateway→supervisor relay 비용이며 작업 자체의 시간이 아니다. 거절 probe마다 반복 횟수를 3회로 고정했고, 실패 후 자동 재시도는 없다. R7은 policy advisor 대기 후 1회만 다시 시도했다. 표본이 작아 p95는 보고하지 않는다.

## 5. Supervisor 메시지 경계 (애플리케이션, OpenShell 무관)

`src/rfa_mas/application/workers.py`의 `SupervisorBus`를 같은 프로세스에서 직접 호출했다. OpenShell 결과와 별개로 판정하며, 한쪽 결과로 다른 쪽 통제를 증명하지 않는다.

| sender → recipient | 기대 | 결정 | 코드 |
| --- | --- | --- | --- |
| paper_scout → experiment_runner | 거절 | denied | `direct_message_denied` |
| experiment_runner → result_analyst | 거절 | denied | `direct_message_denied` |
| paper_scout → supervisor | 전달 | delivered | — |
| supervisor → experiment_runner | 전달 | delivered | — |
| supervisor → supervisor | 거절 | denied | `direct_message_denied` |

전달 기록은 `[paper_scout→supervisor, supervisor→experiment_runner]` 두 건뿐이다. 소스에 DebateLease 구성이 없으므로(`debate_lease_construct_in_src: false`) lease 없는 상태에서 측정했다.

## 6. 관측된 제한과 해석

- **자기 권한 확대**: 거절 후 policy advisor가 제안 2건을 pending으로 만들었지만, 기본 manual 승인 모드라 승인되지 않았고 재시도는 계속 거절되었다. sandbox 안에는 `openshell` CLI가 없다. worker가 gateway API를 직접 호출하는 시도는 이번 matrix에 넣지 않았다.
- **R4b (process 계보 상속)**: network rule의 binary 판정은 승인된 binary의 자손 process도 허용한다. 승인된 python3.12가 띄운 bash는 같은 허용 endpoint에 접속했다. 허용 endpoint 밖으로 확대된 것은 아니지만, "binary 단위 제한"을 interpreter 전체의 권한으로 해석해야 한다.
- **E6 (workdir 실행)**: 경로 정책은 쓰기 가능한 workdir 안에 복사한 binary의 실행을 막지 않았다. 임의 실행 차단은 승인 경로 밖(E3)에 한정해 주장한다.
- **R6**: VM driver는 host 파일시스템을 sandbox에 노출하지 않는다. host 경로 접근이 실패한 원인은 부재이며 정책 거절 증거가 아니다. 그래서 sentinel을 stand-in rootfs에 넣고 정책 밖 경로에서 거절을 판정했다(R5).
- **Endpoint 수명**: 탐색 중 한 번은 합성 HTTP 서버가 이미 종료된 상태였다. 이때 허용 요청이 `NET:FAIL`로 실패했고, 판정 규칙대로 거절로 세지 않았다. orchestrator는 서버를 같은 프로세스 thread로 띄우고 probe 전후로 health를 확인한다.
- **host 비루프백 IP**: 문서대로 host 비루프백 IP와 `allowed_ips: [<ip>/32]`(명시적 private-host trust)를 사용했다. brew service로 실행한 gateway에서도 LAN IP 연결이 동작했다.

## 7. 재현 절차

1. OpenShell v0.1.1 gateway(Homebrew formula, `brew services start nvidia/openshell/openshell`). `~/.config/openshell/gateway.toml`의 설정은 `compute_driver = "vm"`, `[openshell.drivers.vm] vcpus = 2, mem_mib = 2048, allow_driver_config = true`다. 마지막 값은 `--from ./rootfs.tar` 생성에 필요한 공식 operator opt-in이며, 없으면 "caller driver config is disabled" 오류가 난다. resource admission은 기본(활성)이다. telemetry는 `OPENSHELL_TELEMETRY_ENABLED=false`다. VM rootfs 준비에는 `e2fsprogs`가 필요하다(P1-007 기록).
2. Stand-in rootfs: Docker daemon에서 `deploy/openshell/build_rootfs.sh /abs/path/rfa-e2e05-rootfs.tar`를 실행한다. base는 `python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`이다. 결과 tar의 sha256은 `57e353aed038cd19d78518287d0fe9710fbad68ba75e8267c3b79552bc2b5851`(145,109,504 bytes)이고 repository 밖에 둔다.
3. 기본 테스트 `.venv/bin/python -m pytest -q tests/integration/test_openshell_live.py`의 결과는 4 passed, 1 skipped다. 건너뛴 live 사유는 `not_run: opt-in real OpenShell E2E-05 …; a skipped live test is not a pass`다.
4. Live 실행: `RFA_OPENSHELL_LIVE=1 RFA_OPENSHELL_ROOTFS=<tar> RFA_OPENSHELL_EVIDENCE_OUT=<dir> .venv/bin/python -m pytest -q tests/integration/test_openshell_live.py`. 또는 `.venv/bin/python scripts/openshell_e2e05.py --rootfs <tar> --out <dir>`를 쓴다(종료 코드 0 passed, 1 failed, 2 not_run). 결과는 `<dir>/report.json`과 `matrix.md`다.

이번 증거 보고서는 repository 밖 `~/.cache/rfa-mas/openshell-e2e05/runs/0926181417/report.json`(sha256 `939d6a0a142ce2f5400070404a3c93f933d5dedb68d6fe6454bfdaac11dd6f96`)에 있다.

## 8. 실행 중 실패와 수정

| 시도 | 관측 | 조치 |
| --- | --- | --- |
| upload로 sentinel을 정책 밖 경로에 두기 | upload도 sandbox 사용자와 Landlock 아래에서 실행되어 정책 밖 경로에 쓸 수 없음 | sentinel을 stand-in rootfs에 포함 |
| 실행 중 filesystem 정책 변경 | 경로 제거와 include_workdir 변경은 시작 후 거절됨(공식 문서) | 역할별·통제별로 별도 sandbox 사용 |
| rootfs tar 생성 | `caller driver config is disabled` | gateway에 `allow_driver_config = true` 설정 |
| sandbox 이름 | 최대 19자 초과로 거절 | `e5-<HHMMSS>-r1` 형식 |
| 실행 역할 script 경로 | 디렉터리 upload가 이름을 유지해 script를 찾지 못함 | `/sandbox`로 upload, `/sandbox/workdir` 사용 |
| R4 (첫 smoke) | python이 띄운 bash가 허용됨(자손 상속) | bash를 exec로 직접 실행해 R4로 두고, 상속 경우는 R4b 정보 행으로 분리 |
| 보고서 검토 | 서버 hit 수에 host health check가 섞였고, advisor 제안이 flush되기 전에 R7을 실행함 | 계수를 health check 이전으로 옮기고 제안 flush 대기 후 재시도(`83fccfb`) |

## 9. host 상태 변경

- Colima는 작업 전 정지 상태였다. rootfs 빌드를 위해 `colima start --cpu 4 --memory 8 --save-config=false`로 시작했다. 이때 restart 정책으로 다른 프로젝트의 기존 container 5개(`agentic-loop-demo2-*`, 2026-07-27 생성)가 자동 시작되었다. host 연결이 없음을 확인한 뒤 그 container들과 Colima를 다시 정지해 원래 상태로 되돌렸다. Colima docker에는 `python:3.12-slim` base image가 남아 있다.
- `~/.config/openshell/gateway.toml`에 `allow_driver_config = true`를 추가했다.
- stand-in rootfs와 보고서는 `~/.cache/rfa-mas/openshell-e2e05/`에 있다.
- 종료 시 OpenShell sandbox 0개를 확인하고 brew service를 정지했다. openshell·krun·합성 HTTP 서버 process가 없고 17670/18081–18083 port가 비어 있음도 확인했다.
- 03:09 KST에 다른 worker가 P1-006C Langfuse(`rfa-lf-p1006c-*`)를 위해 Colima를 다시 시작했다. 이 작업은 그 VM과 container를 건드리지 않았고 실행 중인 상태로 두었다.

## 10. not_run

- teammate runtime·runtime identity 연동(P1-008B)과 P1-007B의 실제 역할 identity 매핑
- RFA 제품 RuntimePort/TeamRunner가 역할을 OpenShell sandbox에서 실행하는 경로. 현재 제품 경로는 LocalRuntime이며 OS sandbox가 아니다.
- NemoClaw 운영 경로(P1-007A)
- Docker·Podman·Kubernetes driver, supervisor middleware, provider credential 주입, TLS endpoint
- Landlock 접근별 감사 로그(OpenShell 0.1.1에 없음). 파일 거절은 policy differential로만 귀속했다.
- Linux host, GPU sandbox, 부하와 p95, real-model 경로
