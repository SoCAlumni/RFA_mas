# NVIDIA 해커톤 제출 양식 — Section 02. 팀을 대표하는 서비스

기준: main, 2026-09-28. 1~5장은 양식 칸에 그대로 붙여 넣는 본문이고, 6~7장은 제출 전에 확인할 내부 참고다.
주장 범위는 [JUDGING_KEYWORDS.md](JUDGING_KEYWORDS.md)의 상태 표기(`LIVE`/`TEST`/`PART`/`NO`)와 8장 정정표를 따른다.

## 1. 서비스 명

**RFA (Request For Approval) — 우리는 결재만 한다**

## 2. 서비스 파일 (또는 배포 URL)

- 팀명: SoCAlumni
- 파일명: `[NVIDIA 해커톤_SoCAlumni_RFA]` (word 또는 pdf)
- 파일에 적을 링크:

| 항목 | 링크 |
| --- | --- |
| 데모 사이트 (결재함 + 비서 채팅) | https://includes-assets-registrar-mines.trycloudflare.com/inbox |
| 웹앱 | https://github.com/SoCAlumni/RFA_webapp |
| 운영층·API (이 저장소) | https://github.com/SoCAlumni/RFA_mas |
| 공개 대응·문서 발행 에이전트 | https://github.com/SoCAlumni/RFA_module |
| 설계·로드맵·샌드박스 정책 | https://github.com/SoCAlumni/RequestForApproval |

저장소마다 맡은 일:

| 저장소 | 맡은 일 |
| --- | --- |
| `RFA_module` | 공개(public) 쪽에서 문서를 발행하는 대응 에이전트. GitHub·Slack 멘션을 받아 head에게 검열된 지식을 요청하고, 답을 써서 결재에 올리고, 승인되면 그 채널에 게시한다. 거절되면 사유를 반영해 다시 쓴다. 결재 서버와 채널 어댑터도 여기 있다 |
| `RFA_mas` | 사내 지식 쪽 운영층. head, 태스크 팀, 브로커, egress-proxy 검열, 보안 그룹 컨트롤러, 감사 로그, 웹앱용 API |
| `RFA_webapp` | 결재함과 비서 채팅 화면 |
| `RequestForApproval` | 등급별 샌드박스 설계, 네트워크 정책, NemoClaw 검증 기록, 로드맵 |

데모 사이트는 Cloudflare Tunnel 임시 주소다. 터널을 다시 띄우면 주소가 바뀌고, 터널을 연 컴퓨터가 꺼지면 열리지 않는다. 터널로 들어온 화면은 게스트 권한이다.

## 3. 해결하고자 했던 문제 (Problem Definition) — 300자 내외

기업이 업무에 AI 에이전트를 쓰려면 사내 자료를 읽혀야 합니다. 그런데 같은 에이전트가 외부 채널에 답변까지 쓰면 미공개 일정, 수치, 내부 주소가 섞여 나갈 수 있습니다. 위협은 외부 공격자가 아니라 프롬프트 인젝션이나 오작동을 일으킨 내 에이전트 자신입니다. 그래서 보안팀은 에이전트 도입을 승인하지 못하고, 담당자는 GitHub 이슈나 고객 문의에 답할 때마다 사내 자료를 직접 찾아 공개해도 되는 내용만 골라 씁니다. RFA는 어느 에이전트가 어디로 나갈 수 있는지, 나가는 내용을 누가 검사하는지, 에이전트가 늘어도 규칙이 유지되는지에 답해 사람은 결재만 하게 만듭니다.

## 4. 서비스 소개 및 주요 기능 (Solution) — 500자 내외

RFA는 에이전트가 초안을 쓰고 사람은 결재만 하는 멀티 에이전트 운영층입니다.

사용자 흐름: 공개 대응 에이전트가 GitHub·Slack 멘션 접수 → head가 담당 태스크 팀 선택 → NemoClaw 샌드박스 안 에이전트가 사내 지식 API에서 근거 조회 → 검열(regex → Nemotron 판정)이 기밀 마스킹 → 대응 에이전트가 검열된 지식으로 초안 작성 → 사람이 결재함에서 승인 또는 거절 → 승인된 본문만 게시 단계로 넘어감. 거절 사유는 다음 초안과 검열에 반영됩니다.

핵심 기능:
1. 보안 그룹: OpenShell 정책 preset을 선언하면 컨트롤러가 샌드박스에 반영합니다.
2. 단일 추론 경로: 모든 추론이 egress-proxy 하나를 지나고, 외부 채널은 요청과 응답을 검열합니다.
3. 에이전트는 결재·게시를 호출할 수 없고, 허용하지 않은 목적지는 샌드박스가 차단합니다.
4. 모든 판정이 감사 로그에 남습니다.

현재는 기본 샌드박스 1개로 동작합니다. 최종 구조는 기밀 등급(공개, 회사 내, 사업부 내, 업무 내부)마다 샌드박스를 나누고, 등급 경계를 넘기 전에 검토하는 것입니다.

## 5. 활용한 핵심 기술 및 AI 모델 (Tech Stack)

### NVIDIA 기술

| 기술 | 버전·모델 | 쓰임 |
| --- | --- | --- |
| NemoClaw | 0.0.124 | 샌드박스 온보딩, 멀티 에이전트 manifest(`onboard --agents`, `agents apply`), 정책 preset(`policy add/remove/exclude/explain`), managed MCP(`mcp add`), 스킬 설치 |
| OpenShell | 0.0.116 | 샌드박스 격리(Landlock, seccomp), L7 네트워크 정책(default-deny), 게이트웨이의 추론 경로와 자격증명 주입 |
| OpenClaw | 2026.7.1 (NemoClaw 관리 이미지) | 에이전트 런타임. `sessions_spawn`, 도구 profile과 deny, 워크스페이스 스킬 |
| Nemotron | `nvidia/nemotron-3.5-lightning-30b-a3b` | 에이전트 추론, head 담당 선택, 검열 LLM 판정 |
| build.nvidia.com API | `https://integrate.api.nvidia.com/v1` (OpenAI 호환) | 모든 추론의 상류 endpoint |
| NVIDIA Agent Skills | SKILL.md 10개 작성 + 공식 스킬 사용 | 작성: 에이전트 도메인 스킬 8개(`task-research`, `task-benchmark`, `task-summarizer`, `task-verifier`, `team-supervisor`, `censor`, `sg-assistant`, `sg-head`), 채널 스킬 `rfa-assistant`, 운영 스킬 `nemoclaw-security-groups`. 사용: 개발 중 공식 NemoClaw 사용자 스킬(`nemoclaw-user-*`) |
| NeMo Retriever Skill | 공식 `nemo-retriever` CLI 26.8.1 | 코어층 Research 경로의 근거 검색 |
| NVIDIA 임베딩 | `nvidia/llama-nemotron-embed-vl-1b-v2` | Retriever 검색 임베딩(hosted) |
| NeMo Agent Toolkit | 1.8 | 코어층 평가. offline smoke만 실행 |

### 전체 기술 스택

| 영역 | 기술 |
| --- | --- |
| 언어·런타임 | Python 3.12, uv, Node.js 22+(NemoClaw CLI), Docker |
| 프런트 | Next.js 16, React 19, TypeScript. Next 서버 프록시(`/api/rfa/*`)로 API에 붙고 SSE를 그대로 전달 |
| 서버 | FastAPI, SSE, Pydantic → OpenAPI 생성, httpx, SQLite |
| 에이전트 오케스트레이션 | 호스트 head(LLM 점수 + 키워드 폴백), 브로커(MCP Streamable HTTP + REST), 팀 supervisor와 멤버, LangGraph(공개 대응 워크플로, 코어층) |
| 채널·결재 | GitHub·Slack 채널 어댑터, 결재 서버(FastAPI), 팀 경계 API 계약(OpenAPI → Redoc) |
| 보안 | 선언형 보안 그룹과 reconcile 컨트롤러, egress-proxy, HMAC in-band 마커, regex → LLM 2단계 검열, 로컬 CA TLS |
| 검색 | 한국어 BM25, NeMo Retriever(LanceDB) |
| 관찰 | JSON-lines 감사 로그(`app`/`audit`/`debug`), 감사 화면, Langfuse(opt-in) |
| 대체 provider | 운영층: Gemini(`RFA_LLM_PROVIDER=gemini`). 공개 대응 에이전트: OpenRouter(기본, Nemotron), NVIDIA, Gemini, Anthropic 중 선택(`RFA_LLM_MODE`) |
| 테스트 | pytest, 목업 e2e(`make mock-e2e`), 데모 스크립트 9개 |

## 6. 현재 구현과 최종 구조 (내부 참고)

양식 본문은 아래 구분을 넘지 않는다. 발표와 질의응답에서도 같다.

| 항목 | 현재 구현 | 최종 구조 (로드맵) |
| --- | --- | --- |
| 샌드박스 | 기본 `rfa-main` 1개 운영. 격리용 `rfa-tasks-none`은 선언만 있고 옵트인 | 기밀 등급마다 샌드박스: `public`, `company`, `division`, `team` |
| 등급 | 요청 단위 2단계(사외 `public` / 사내 `company`·`self`) + 지식 노트 공개 등급 | 독자 범위 4단계. 같은 등급 안의 구획은 `team-orbit`처럼 이름 뒤에 붙임 |
| 기밀 검토 위치 | 호스트 egress-proxy와 최종 답 판정 | 높은 등급 샌드박스에서 검토하고, 낮은 등급은 통과한 내용만 받음 |
| 추론 | 모든 alias가 hosted Nemotron | 기밀 등급은 로컬 추론, 공개 등급은 NVIDIA Endpoints. 등급별 gateway 분리 |
| 샌드박스 간 통신 | 호스트 브로커(managed MCP, REST 폴백) | 호스트 서비스 API로만. 열 경로는 네트워크 정책이 정함 |
| 공개 대응 에이전트 | `RFA_module`이 호스트에서 돈다. 결재 서버 연동까지 확인했고, 게시 기본값은 mock(`RFA_PUBLISHER=mock`) | `public` 샌드박스 안에서 돈다. 기밀은 들어오지 않고, 승인된 본문과 대상을 다시 대조한 뒤 실제 채널에 게시 |
| 자료 | 합성 fixture만 | 등급별 추론 분리 뒤 실제 자료 |

최종 구조의 근거는 RequestForApproval 저장소의 `docs/architecture-decisions.md`(D1, D10~D14)와 `docs/milestone.md`에 있다.

## 7. 제출 전 확인

| 확인할 것 | 상태 |
| --- | --- |
| 3장 글자 수 | 공백 포함 약 325자 |
| 4장 글자 수 | 공백 포함 약 580자. 양식이 엄격하면 핵심 기능 4번을 뺀다 |
| 저장소 공개 범위 | 2026-09-28 기준 `RFA_module`만 PUBLIC이다. `RFA_mas`, `RFA_webapp`, `RequestForApproval`은 PRIVATE이라 심사자가 열 수 없다. 공개로 바꾸거나 심사자를 초대한다 |
| 데모 사이트 주소 | 임시 터널 주소다. 심사 기간에 터널과 호스트 서비스를 계속 켜 두고, 주소가 바뀌면 제출 파일도 고친다 |
| "등급별 샌드박스"를 구현 완료로 쓰지 않았는가 | 본문은 "최종 구조"로만 적었다 |
| 게시를 라이브로 쓰지 않았는가 | 본문은 "게시 단계로 넘어감"으로 적었다 |
