# NemoClaw·OpenShell 지원 경로 증거 (P1-007)

이 문서는 P1-007의 설계 산출물이다. 공식 문서로 확인한 사실, 이 Mac에서 실제로 관측한 사실, 아직 실행하지 않은 설계를 구분해 기록한다. 확인일은 2026-09-27 KST(2026-09-26 UTC)이며 확인자는 P1-007 research worker(claude-opus-5-5)다.

이 문서의 어떤 내용도 NemoClaw 운영 경로에서 RFA API가 호출되었다는 증거가 아니다. 그 증거는 P1-007A, RFA 역할별 OpenShell allow/deny 증거는 P1-007B가 담당하며 둘 다 not_run이다. §8의 로컬 관측은 **OpenShell 단독** sandbox에 대한 합성 probe이며 RFA 서비스, NemoClaw, NVIDIA 모델 결과와 무관하다.

## 1. 요약

| 항목 | 판정 | 근거 종류 |
| --- | --- | --- |
| NemoClaw가 임의 LangGraph/FastAPI 앱을 agent runtime으로 수용 | 아니다. 지원 agent는 OpenClaw, Hermes, LangChain Deep Agents Code뿐이며 목록 외 harness는 Unsupported | 공식 문서 |
| RFA 서비스 배치 | sandbox 밖 host 프로세스. sandbox 안 agent가 제한된 기존 REST route를 호출하는 구조로 설계 | 설계(미실행) |
| 이 Mac에서 OpenShell 단독 실행 | 가능. OpenShell 0.1.1 MicroVM driver로 sandbox 생성, 기본 정책의 network 거부를 정책 로그로 확인 | 로컬 관측 |
| 이 Mac에서 NemoClaw quickstart | 실행하지 않음. inference provider credential 또는 대형 로컬 모델, 8 GiB 이상 container runtime이 필요 | 공식 문서 + 판단 |
| P1-007A (NemoClaw 운영 경로 실시연) | blocked 유지. 위 NemoClaw blocker와 선행 P1-008B blocked | task 원본 + 본 문서 |
| P1-007B (RFA 역할별 OpenShell allow/deny) | blocked 유지. OpenShell primitive는 이 Mac에서 재현 가능하지만 RFA runtime identity/역할 경로(P1-008B)가 없음 | 로컬 관측 + task 원본 |

## 2. 공식 지원 runtime·platform·version

### OpenShell

- 최신 stable은 **v0.1.1**이다. GitHub release 게시 시각은 2026-09-26T03:45:16Z이고 문서 사이트도 "Latest (v0.1.1)"로 표시한다. 문서의 Get Started는 `install.sh` 설치 후 `openshell sandbox create` 두 명령이다.
- CLI·gateway 지원 host: Linux(Debian/Ubuntu) amd64·arm64 Supported, **macOS (Docker Desktop) Apple Silicon Supported**, Windows WSL 2 + Docker Desktop Experimental. Intel Mac용 release asset은 없다.
- Sandbox runtime: Docker(Docker Desktop 또는 Docker Engine 28.0+), Podman 5.x(rootless), Kubernetes 1.29+(Helm), **MicroVM**(libkrun, macOS는 Hypervisor.framework, Linux는 KVM). VM driver는 자동 감지되지 않으며 `compute_driver = "vm"`으로 명시해야 한다. 기본 크기는 2 vCPU, 2048 MiB, overlay 4096 MiB다.
- Sandbox 경계에는 Linux Landlock ABI 3 이상(Linux 6.2+)과 seccomp user notification이 필요하다. macOS에서는 이 커널 기능이 Linux VM 안에서 동작한다.
- 기본 workload image는 `nvcr.io/nvidia/base/ubuntu:24.04`다. 기본 정책은 egress deny이며 loopback, link-local, metadata 주소는 항상 차단되고 private 주소도 기본 차단된다(SSRF 방지).
- Supervisor middleware는 network policy 검사 **뒤**, 외부 전달 **전에** sandbox 요청·응답을 검사·차단·변경한다. Middleware 장애 시 기본 fail-closed이며 API는 아직 변경 중이다. Middleware는 sandbox egress 트래픽만 보며 sandbox 밖 프로세스의 트래픽은 보지 않는다.
- OpenShell은 익명 telemetry를 기본 수집하며 gateway 환경의 `OPENSHELL_TELEMETRY_ENABLED=false`로 끌 수 있다.

### NemoClaw

- 문서의 Project Status는 stage **alpha**, label **Early preview**(2026-03-16부터)다. 문서 release notes의 최신 항목은 **v0.0.129**(2026-09-23)다. GitHub releases API의 latest release는 비어 있었다.
- 제품 범위: one host의 trusted operator용 early-preview reference stack이다. hosted service, multi-tenant control plane, enterprise identity system이 아니다.
- 지원 agent: OpenClaw(Tested, 기본), Hermes(Tested), LangChain Deep Agents Code(Tested). "Other LangChain, AutoGen, CrewAI, or non-listed agent harnesses"는 **Unsupported**다. 따라서 RFA의 LangGraph 앱을 NemoClaw agent runtime으로 올리는 공식 경로는 없다.
- Platform: Linux + Docker Tested(Ubuntu 24.04 primary), DGX Spark Tested, **macOS Apple Silicon + Colima 또는 Docker Desktop Tested with limitations**, Windows WSL2 Tested with limitations, Intel Mac Unsupported.
- Hardware: 최소 4 vCPU, 8 GB RAM, 20 GB 여유 disk이고 권장 RAM은 16 GB다. Sandbox image는 압축 약 2.4 GB다. Onboarding preflight는 container runtime이 4 vCPU 또는 8 GiB 미만이면 경고하며 interactive 기본값은 중단이다. Colima 예시는 `colima start --cpu 4 --memory 8`이다.
- Software: Node.js 22.19+, npm 10+, Python 3, container runtime.
- Onboarding에는 inference provider가 필요하다. Hosted provider(NVIDIA Endpoints, OpenRouter, OpenAI, Anthropic, Gemini, Model Router)는 각 API key가 필요하다. Local Ollama는 Tested with limitations이며 검증된 기본 모델은 `qwen3.6:35b`, `nemotron-3-nano:30b`, `qwen3.5:9b`다.
- Managed MCP는 authenticated **HTTPS Streamable HTTP MCP** endpoint만 받는다. Loopback, `host.openshell.internal`, `host.docker.internal`은 거절된다. Private host는 RFC1918·CGNAT·IPv6 ULA에 대한 명시적 `--trusted-private-host` 선언이 필요하다. 현재 managed MCP는 pinned OpenShell **0.0.116**을 요구하므로 NemoClaw가 관리하는 OpenShell 버전은 단독 최신 0.1.1과 다르다. NemoClaw 환경에서는 `openshell sandbox create`나 self-update를 직접 쓰지 말고 `nemoclaw onboard`를 쓰라고 안내한다.
- Host 서비스 호출: sandbox가 host HTTP 서비스를 호출하려면 서비스를 non-loopback host IP에 노출하고, 그 IP·port·method·path·binary를 적은 custom preset을 `nemoclaw <sandbox> policy add --from-file ... --trusted-private-host <ip>`로 적용한다. `host.openshell.internal`/`host.docker.internal`은 일반 경로로 보장되지 않는다. 사용자 preset에서 `allowed_ips`는 거절된다.

### DLI 과정

- 교육 미션 URL(learn.nvidia.com, `course-v1:DLI+S-FX-43+V1`)은 HTTP 200을 반환했다. 하지만 과정 본문 필드가 JavaScript로 채워져 정적 조회에서 비어 있었고, in-app browser도 사용할 수 없어 **페이지 본문을 직접 확인하지 못했다**.
- NVIDIA Deep Learning Institute의 `NVDLI/NemoClawDLI` 저장소(main `697623e287c5`, 2026-09-23 commit) README는 위 course ID를 "official NVIDIA DLI course page"로 링크한다. README는 과정 저장소가 NemoClaw와 그 runtime을 외부 dependency로 둔다고 명시하며, Module 3 live-agent 실습은 NemoClaw Brev launchable(원격)을 사용한다. `web/nemoclaw/COURSE_CANON.md`는 과정 제목 "Securing Agents with OpenShell and NemoClaw"와 학습 목표 5개를 verbatim canonical로 정의한다. 교육 매핑은 이 목표만 사용하며 미확인 제출 미션, 필수 기술 수, 수료 조건은 추가하지 않는다.

## 3. NemoClaw agent가 쓸 최소 호출 API

기존 `src/rfa_mas/api/app.py` route만 사용한다(worktree 기준 main `d3cad28`). 새 route, MCP wrapper, 계약 변경은 없다. 별도 MCP wrapper는 승희 경계가 제공할 때 재검토한다. 현재는 HTTPS Streamable HTTP MCP endpoint가 없으므로 NemoClaw managed MCP 경로는 적용할 수 없다.

| Route | Agent 허용 | 이유 |
| --- | --- | --- |
| `GET /healthz` | 허용 | 인증 없는 상태 확인. 본문 데이터 없음 |
| `POST /v1/sessions` | 허용 | 서버가 owner/thread를 발급한다. body의 소유권 주장은 422 |
| `POST /v1/sessions/{session_id}/work` | 허용 | `DirectWorkRequest` 1.1 → `RunResult` 1.0. 서버가 run_id를 새로 발급하고 멱등 key를 보존한다 |
| `GET /v1/runs/{run_id}` | 허용 | 진행 중 run 상태 조회(`RunRecord` 1.1) |
| `GET /readyz`, `POST /v1/work`, `GET /v1/work/{run_id}` | 제외 | 기능이 위 경로와 겹친다. 최소 집합을 유지한다 |
| `GET /v1/sessions`, `GET /v1/sessions/{session_id}` | 제외 | owner의 다른 세션 목록과 대화 이력이 노출된다 |
| `POST /v1/runs/{run_id}/resume` | 제외 | 승인 원본 재조회 신호는 owner/승인 흐름 책임이다 |
| `/v1/knowledge/sources*`(POST/GET/PUT/DELETE), `/v1/knowledge/imports`, `/v1/knowledge/derive`, `/v1/knowledge/derived` | 제외 | KB 원문 읽기·쓰기·삭제는 owner 관리 작업이다 |
| `/v1/candidates/discover`, `/v1/candidates`, `/v1/candidates/{candidate_id}/decision` | 제외 | 후보 결정은 owner의 판단이다 |

## 4. Identity

사실(소스): `resolve_principal`은 `APP_API_KEY`가 설정되면 `Authorization: Bearer <key>`를 constant-time 비교해 **단일 설치 owner** principal을 반환한다. Key가 없으면 forwarded header가 없는 직접 loopback 연결만 허용한다. `Settings`는 non-loopback bind일 때 `APP_API_KEY`를 필수로 만든다.

사실(공식 문서): OpenShell은 loopback 목적지를 항상 차단하고 NemoClaw도 loopback을 거절한다. 따라서 sandbox agent가 RFA를 호출하려면 RFA를 non-loopback 주소에 bind해야 하며, 그러면 keyless 개발 모드는 쓸 수 없다.

결과와 한계:

- Agent 요청은 installation owner로 인증되므로 허용된 route에서 owner 권한을 갖는다. Agent 전용 principal이나 scope는 없다.
- `DirectWorkRequest.target.audience`는 caller가 정한다. OpenShell REST rule은 method·path·query만 검사하고 JSON body는 검사하지 않으므로 audience를 제한하지 못한다. Audience별 자료 제한은 기존 application policy(`LocalPolicy`, 공개 근거 선필터, private canary 차단)가 계속 책임진다.
- Agent 전용 scoped principal은 인증·계약 변경이므로 coordinator 절차가 필요하다. 이 task에서는 구현하지 않았다.
- Credential 전달 설계(미실행): endpoint 없는 profile의 OpenShell provider를 만들고 RFA endpoint에 `credential_binding.provider`를 붙인다. Agent는 `openshell:resolve:env:KEY` placeholder만 보고, proxy가 bound host·port·path 요청의 `Authorization` header에서만 실제 값으로 바꾼다. 다른 목적지로 보내면 `credential_endpoint_mismatch`로 거절된다. 이 key는 데모용으로 새로 발급한 값이어야 하며 저장소 `.env` 값을 쓰지 않는다.
- OpenShell의 sandbox identity(sandbox ID, uid, policy version/hash)는 RFA `TrustedPrincipal`과 연결되어 있지 않다. 둘을 결합하는 것은 P1-008B/P1-007B의 runtime identity 작업이다.

## 5. Egress allowlist

Sandbox 쪽 설계(미실행)는 기본 deny 위에 RFA endpoint 하나만 추가한다. NemoClaw custom preset 형식은 다음과 같다. `<HOST_PRIVATE_IP>`와 agent binary 경로는 실제 환경의 OCSF 로그로 확정해야 하는 값이다.

~~~yaml
preset:
  name: rfa-api-minimal
  description: "RFA MAS minimal API (synthetic demo)"
network_policies:
  rfa_api:
    name: rfa_api
    endpoints:
      - host: <HOST_PRIVATE_IP>
        port: 8000
        protocol: rest
        enforcement: enforce
        rules:
          - allow: { method: GET, path: "/healthz" }
          - allow: { method: POST, path: "/v1/sessions" }
          - allow: { method: POST, path: "/v1/sessions/*/work" }
          - allow: { method: GET, path: "/v1/runs/*" }
    binaries:
      - { path: <AGENT_HTTP_CLIENT_BINARY> }
~~~

OpenShell path glob에서 `*`는 한 segment 안에서만 match한다. 적용은 `nemoclaw <sandbox> policy add --from-file rfa-api-minimal.yaml --trusted-private-host <HOST_PRIVATE_IP> --dry-run`으로 생성된 pin을 검토한 뒤 수행한다. Credential binding(§4)은 NemoClaw preset 형식에서 지원되는지 확인하지 않았다. 확인이 필요한 미검증 항목이다. Inference egress는 NemoClaw가 `inference.local` route로 따로 관리한다.

RFA는 plain HTTP(uvicorn, TLS 없음)로 제공된다. OpenShell supervisor에서 RFA까지 Bearer token이 host private 주소 위 평문으로 흐른다. 가능한 한 외부에서 도달할 수 없는 host 인터페이스를 쓰고 방화벽으로 제한해야 한다. 이 선택지는 검증하지 않았다.

Backend 쪽 egress는 OpenShell 정책의 대상이 아니다. 현재 application gate만 있다. P0 HTTP adapter는 loopback만 허용하고, `external_egress_effective`와 `external_writes_effective`는 항상 false다. `MODEL_PROVIDER=nvidia`이면 backend가 NVIDIA hosted API를 host에서 직접 호출한다.

## 6. 배치 경계와 보호되지 않는 범위

~~~mermaid
flowchart LR
  subgraph Host["host (일반 사용자 권한, sandbox 밖)"]
    RFA["RFA FastAPI + LangGraph<br/>uvicorn :8000"]
    DB[("SQLite DB, checkpoint,<br/>trace, .env")]
    GW["OpenShell gateway<br/>127.0.0.1:17670"]
    RFA --- DB
  end
  subgraph SB["OpenShell sandbox (NemoClaw 관리 시)"]
    AG["Agent (OpenClaw 등 지원 runtime)"]
  end
  AG -- "policy 허용 route만<br/>(REST method/path 검사)" --> RFA
  RFA -- "OpenShell 비적용<br/>(application gate만)" --> EXT["NVIDIA API, 팀원 서비스"]
  GW -. "policy, credential, logs" .- SB
~~~

RFA backend process는 **어떤 sandbox 안에서도 실행되지 않는다**. 이 문서의 설계에서 이를 바꾸지 않으며, sandbox 안 실행을 입증한 증거도 없다. OpenShell/NemoClaw가 강제하는 것은 sandbox 안 agent 프로세스의 파일·프로세스·network 접근과 credential placeholder 해석뿐이다. 다음은 보호되지 않는다.

- **파일**: RFA process는 사용자 계정 권한으로 저장소 `.env`(NVIDIA_API_KEY, APP_API_KEY 등), `.local/` SQLite DB(KB 원문, private note, 세션), LangGraph checkpoint, trace 디렉터리 전체에 접근한다. Landlock이나 filesystem policy가 없다. DB 파일 0600 검사는 같은 사용자에 대한 sandbox가 아니다.
- **Network**: RFA의 outbound 연결(NVIDIA hosted API, Retriever, 팀원 loopback 서비스, Langfuse)은 OpenShell egress policy, middleware, OCSF 로그 밖이다. Application gate가 유일한 통제다.
- **Process**: LangGraph node, `LocalRuntime`이 같은 process에서 실행하는 domain task, tool 계산에는 seccomp, process 수 제한, 권한 하강이 없다.
- **Credential**: RFA process는 raw key를 메모리에 보유한다. OpenShell provider의 credential custody는 sandbox agent의 요청에만 적용된다.
- **반환 데이터**: RFA가 sandbox로 반환한 본문은 agent가 읽을 수 있다. 어떤 자료를 반환할지는 RFA의 authorization·audience 정책이 정하며, OpenShell은 응답 내용을 검사하지 않는다(middleware를 설정하지 않은 경우). 반환 후 외부 유출은 sandbox egress policy가 막는다.
- **권한 범위**: Agent는 owner key로 인증하므로 허용 route 안에서는 owner와 같은 권한이다(§4).
- **감사**: OCSF 로그는 sandbox 쪽 연결·요청 결정만 기록한다. RFA 내부 동작은 P1-006D observation ledger에만 남으며 두 기록은 자동으로 연결되지 않는다.
- **Prompt injection**: 검색 근거나 tool 결과에 포함된 지시는 agent에 전달될 수 있다. OpenShell은 그 결과 agent가 할 수 있는 호출을 허용 route와 egress로 제한할 뿐이다.

## 7. 안전한 합성 입력 계획 (P1-007A/B 실행 시)

1. **격리된 설정**: `uv run rfa init-env --output <tmpdir>/.env.p1007`로 서로 다른 로컬 credential을 새로 만든 0600 profile을 사용한다. 저장소 `.env`는 읽지 않는다. `MODEL_PROVIDER=mock`, 기본 mock/local backend, `DATABASE_URL`은 임시 디렉터리로 두어 NVIDIA key 없이 실행한다.
2. **자료**: 설치 시 seed되는 합성 fixture(`synthetic-fixtures-v1`), `fixtures/documents/*.jsonl`, `fixtures/imports/*.json`만 사용한다. 실제 노트, 개인정보, 사내 자료는 넣지 않는다.
3. **허용 probe**: `GET /healthz` → `POST /v1/sessions` → `POST /v1/sessions/{session_id}/work`(고정 합성 질의, target audience owner 1회와 public 1회) → `GET /v1/runs/{run_id}`. Public 응답에는 기존 private canary가 없어야 한다. `RunResult.simulated=true`와 mock adapter 목록을 그대로 기록하고 NVIDIA 결과로 보고하지 않는다.
4. **거부 probe**: `POST /v1/knowledge/sources`, `DELETE /v1/knowledge/sources/{source_id}`, `POST /v1/candidates/{candidate_id}/decision`, `GET /v1/sessions`, 그리고 `example.com:443` 연결. OpenShell `policy_denied` 응답과 해당 OCSF DENIED 기록이 **함께** 있을 때만 정책 거부로 집계한다. RFA의 401/404/422, 연결 실패, `upstream_unreachable`은 정책 거부로 세지 않는다.
5. **기록 범위**: method, path, HTTP status, OCSF 결정·reason, policy version/hash, sandbox ID, run status만 기록한다. 요청·응답 본문(합성 질의 제외), header, token, placeholder 값은 기록하지 않는다.
6. **중단 조건**: 거부 probe가 2xx를 반환하면 즉시 중단하고 실패로 기록한다. 같은 실패 원인에서는 최대 3 cycle까지만 수정한다.

## 8. 이 Mac의 실행 가능성과 로컬 관측

### 환경

macOS 26.5.2(25F84), arm64 Apple M4, RAM 16 GiB, 여유 disk 84 GiB, NVIDIA GPU 없음. Colima 0.10.1이 설치되어 있지만 정지 상태였다(profile: 4 CPU, 8 GiB). Docker CLI 29.4.0은 daemon에 연결되지 않은 상태다. Homebrew 6.0.14였다가 installer 실행 중 7.0.6으로 자동 update되었다.

### 경로별 판정

| 경로 | 공식 지원 | 판정 |
| --- | --- | --- |
| OpenShell 0.1.1 단독 + MicroVM driver | macOS Apple Silicon host Supported, MicroVM runtime Supported(Hypervisor.framework) | **시도, 성공**. 문서에 없는 의존성 `e2fsprogs`가 1회 필요했다 |
| OpenShell 0.1.1 단독 + Colima Docker | OpenShell support matrix의 macOS 항목은 Docker Desktop만 명시하며 Colima는 목록에 없다. v0.1.1 Docker driver의 socket 자동 감지는 `DOCKER_HOST`, `/var/run/docker.sock`, `~/.docker/run/docker.sock`, `XDG_RUNTIME_DIR`만 보며 Colima의 `~/.colima/default/docker.sock`은 포함하지 않는다 | 공식 quickstart 범위 밖이라 **시도하지 않음**. Colima를 시작하지 않았다 |
| NemoClaw quickstart(OpenClaw) | macOS + Colima Tested with limitations | **시도하지 않음**. 아래 blocker |

NemoClaw blocker:

- Onboarding은 inference provider를 요구한다. Hosted 선택지는 API key가 필요하다. 저장소 `.env`의 NVIDIA_API_KEY는 이 task에서 사용 승인 범위 밖이다.
- Credential 없는 대안인 Local Ollama는 가장 작은 검증 모델(`qwen3.5:9b`)도 host 메모리를 크게 쓴다. 문서의 container runtime 최소치(4 vCPU/8 GiB)와 합치면 16 GiB 장비에서 여유가 없다.
- 이 task의 Colima 상한(처음 4 CPU/6 GB, 이후 공유 조건 4 CPU/8 GB)은 NemoClaw 최소치 이하이거나 같다. 같은 VM을 다른 worker의 Langfuse와 공유해야 한다.
- 압축 약 2.4 GB image와 20 GB 이상 disk가 필요하다. "lightweight quickstart" 조건에 맞지 않는다.

NemoClaw quickstart가 성공하더라도 P1-007A에는 §3~§5의 RFA non-loopback bind, custom preset, credential binding과 선행 P1-008B가 추가로 필요하다.

### 실행 기록 (OpenShell 단독, 합성 데이터)

시각은 2026-09-26 17:15–17:18 UTC(2026-09-27 02:15–02:18 KST)다. Provider와 credential은 사용하지 않았고 `.env`는 읽지 않았다.

1. 설정 파일 2개를 작성했다. 둘 다 비밀 값이 없다.
   - `~/.config/openshell/gateway.toml`: `[openshell] version = 2`, `[openshell.gateway] compute_driver = "vm"`, `[openshell.drivers.vm] vcpus = 2, mem_mib = 2048`
   - `~/.config/openshell/gateway.env`: `OPENSHELL_TELEMETRY_ENABLED=false`. Formula의 service wrapper가 이 파일을 읽는다. Telemetry 비활성의 실제 효과는 로그로 따로 확인하지 못했다.
2. 설치: `OPENSHELL_VERSION=v0.1.1 sh install.sh`. 이 `install.sh`는 main에서 받은 것이며 sha256은 `5c98a86a4b811c471b212219cb2a62d458244220ffa71ac8e3baf3700b17b871`이다. 결과는 다음과 같다.
   - `openshell 0.1.1`, `openshell-gateway 0.1.1`, `openshell-prover 0.1.1`
   - brew service `sh.brew.openshell` 시작
   - `openshell status`: `Connected`, `Authenticated (mTLS transport)`, `Version: 0.1.1`
   - Gateway 로그: `Using compute driver driver=vm`, `Gateway listener bound address=127.0.0.1:17670`
3. 시도 1: `openshell sandbox create --name p1007-probe --no-auto-providers --detach -- sleep 900`이 `ProvisioningFailed: failed to create ext4 rootfs image ...: mke2fs not found ... Install e2fsprogs`로 실패했다. 관련 공개 issue는 NVIDIA/OpenShell#3400(open)과 PR #3477이다.
4. 수정: `brew install e2fsprogs`(1.47.4, keg-only) 후 같은 명령을 다시 실행했다(retry cycle 1/3). 21.6초 만에 `Ready`가 되었다(image `nvcr.io/nvidia/base/ubuntu:24.04`, layer 27 MB + 5 MB).
5. 관측:

| 확인 | 관측 | 해석 |
| --- | --- | --- |
| Sandbox 상태 | ID `202fc1c9-b898-4f37-8b1b-72f5d49bc4b9`, `Ready: True (DependenciesReady) - Supervisor session connected`, policy source `sandbox` revision 1 | Gateway·supervisor 연결 확인 |
| Guest | `uid=1000(ubuntu)`, `Linux 6.12.76 aarch64`, `Ubuntu 24.04.5 LTS`, cwd `/sandbox`, network interface `lo`만 존재 | Non-root, VM에 NIC 없음(문서와 일치) |
| 정책 | filesystem read_only `/bin /usr /lib /proc /dev/urandom /etc /var/log`, read_write `/tmp /dev/null`, `include_workdir: true`, `landlock.compatibility: best_effort`, `network_policies` 없음. `CONFIG:LOADED ... [version:1] [hash:a45a8ff3564f00ae26cbd66f1131f37eedd8b54a66fbfec103d65a204c19c6e8]` | 기본 정책 |
| 파일 쓰기 | `/sandbox/p1007-ok.txt` 성공. `/etc/p1007-denied.txt`는 `Permission denied` | `/etc` 실패에 대한 정책 이벤트가 없다. root 소유 `/etc`에 대한 uid 1000의 Unix 권한으로도 설명되므로 **정책 거부로 판정하지 않음** |
| Network | `bash` `/dev/tcp/example.com/443` → `connect: Permission denied`. OCSF: `NET:REFUSE [MED] DENIED example.com [reason:policy_dns_ineligible]`, `NET:OPEN [MED] DENIED /usr/bin/bash(0) -> example.com:443 [reason:transparent_tcp_policy_denied]` | 실패와 정책 결정 로그가 결합되어 **정책 거부로 판정** |
| Policy advisor | pending chunk `allow_example_com_443`(bash → example.com:443 L4, `prover: no new findings`) | 승인하지 않았고 sandbox와 함께 삭제했다 |

6. 정리: `openshell sandbox delete p1007-probe` → `No sandboxes found`. `brew services stop nvidia/openshell/openshell` 후 openshell·krun process가 없고 17670 listen이 해제된 것을 확인했다. Installer가 켠 Homebrew developer mode는 `brew developer off`로 되돌렸다. Colima는 이 작업에서 시작하지 않았다.

남은 host 변경: Homebrew 7.0.6 자동 update(되돌리지 않음), 설치된 `openshell`(nvidia/openshell 로컬 tap, service 중지)과 `e2fsprogs`, `~/.config/openshell/`(위 설정과 CLI gateway 등록), `~/.local/state/openshell`(약 217 MB image·TLS 상태). 제거할 때는 공식 문서 순서대로 `brew services stop nvidia/openshell/openshell`, `brew uninstall nvidia/openshell/openshell`, `rm -rf "$(brew --prefix)/var/openshell"`를 실행한다. 필요하면 `brew uninstall e2fsprogs`와 위 두 디렉터리 삭제를 이어서 한다. 이후 NemoClaw onboarding은 자체 pinned OpenShell formula를 검증·복구하므로 단독 0.1.1 설치와 충돌할 수 있다.

## 9. not_run

- NemoClaw 설치·onboarding·OpenClaw sandbox 실행과 이 서비스 호출(P1-007A AC1~3)
- RFA API를 non-loopback에 띄우고 OpenShell/NemoClaw preset·credential binding으로 호출하는 경로
- RFA 역할별 identity와 파일·network·tool allow/deny matrix(P1-007B AC1~3), RuntimePort 정규화 결과와 강제 로그 연결
- Supervisor middleware 적용, Docker/Colima driver 경로, Kubernetes 경로
- OpenShell filesystem policy(Landlock) 거부를 정책 로그로 입증하는 probe. 이번 `/etc` 실패는 판정 불가였다.
- learn.nvidia.com 과정 페이지 본문 직접 확인

## 공식 출처

확인일은 모두 2026-09-27 KST다.

- [OpenShell docs index(llms.txt)](https://docs.nvidia.com/openshell/llms.txt), [Overview](https://docs.nvidia.com/openshell/about/why-open-shell), [Support Matrix](https://docs.nvidia.com/openshell/about/support-matrix), [Installation](https://docs.nvidia.com/openshell/about/installation), [Architecture](https://docs.nvidia.com/openshell/about/architecture), [Sandbox Runtimes](https://docs.nvidia.com/openshell/how-it-works/sandboxes/runtimes), [Manage Sandboxes](https://docs.nvidia.com/openshell/how-it-works/sandboxes/overview), [Gateway Configuration](https://docs.nvidia.com/openshell/how-it-works/gateways/configuration), [Network Rules](https://docs.nvidia.com/openshell/how-it-works/policies/network-rules), [Policy Schema](https://docs.nvidia.com/openshell/how-it-works/policies/schema), [Providers](https://docs.nvidia.com/openshell/how-it-works/providers/overview), [Supervisor Middleware](https://docs.nvidia.com/openshell/extensibility/supervisor-middleware), [First Network Policy](https://docs.nvidia.com/openshell/tutorials/first-network-policy), [Run Your First Agent](https://docs.nvidia.com/openshell/about/run-your-first-agent), [Telemetry](https://docs.nvidia.com/openshell/observability/telemetry)
- [OpenShell v0.1.1 release](https://github.com/NVIDIA/OpenShell/releases/tag/v0.1.1), [install.sh](https://github.com/NVIDIA/OpenShell/blob/main/install.sh), [v0.1.1 Docker driver source](https://github.com/NVIDIA/OpenShell/blob/v0.1.1/crates/openshell-driver-docker/src/lib.rs), [v0.1.1 VM driver defaults](https://github.com/NVIDIA/OpenShell/blob/v0.1.1/crates/openshell-driver-vm/src/driver.rs), [issue #3400](https://github.com/NVIDIA/OpenShell/issues/3400)
- [NemoClaw docs index(llms.txt)](https://docs.nvidia.com/nemoclaw/latest/llms.txt), [Overview](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/about/overview), [Ecosystem](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/about/ecosystem), [Platform Support](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/reference/platform-support), [Prerequisites](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/get-started/prerequisites), [Quickstart](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/get-started/quickstart), [Release Notes](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/release-notes), [About Managed MCP Servers](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/manage-sandboxes/mcp-servers/about-managed-mcp-servers), [Add an MCP Server](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/manage-sandboxes/mcp-servers/add-an-mcp-server), [Create Custom Policy Presets](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/network-policy/configure-policies/create-custom-policy-presets), [Troubleshooting: host-side HTTP service](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/reference/troubleshooting)
- [DLI 과정 페이지](https://learn.nvidia.com/courses/course-detail?course_id=course-v1:DLI+S-FX-43+V1)(본문 미확인), [NVDLI/NemoClawDLI README](https://github.com/NVDLI/NemoClawDLI), [COURSE_CANON.md](https://github.com/NVDLI/NemoClawDLI/blob/main/web/nemoclaw/COURSE_CANON.md)
