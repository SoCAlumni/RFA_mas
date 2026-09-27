# 프런트 연동 2차: 사이드바 「에이전트」·「샌드박스」 관리 화면

- 기준 UI: 시안 3차 판(artifact `LFD3Utajj2ahC9wv45MbAg`, `1790527401-f612`). 사이드바 「관리」 아래 「에이전트 N」·「샌드박스 N」 버튼이 관리 팝업을 그 페이지로 연다.
- 결정: D-16(실제 샌드박스)·D-17(백엔드 에이전트 전부, 태스크 에이전트 먼저, 관리 에이전트는 읽기 전용)·D-18(즉시 적용만 실제 동작)·D-19(프록시 실측 컨텍스트) — `docs/decisions.md`.
- 계약 원본: `docs/openapi.yaml` (tag `admin`). 오류 본문은 `{code, message}`(바로 보여 줄 한국어).

## 1. 요구사항

### 에이전트 페이지

| ID | 요구사항 | API |
|---|---|---|
| AG-1 | 사이드바 「에이전트 N」 | `GET /admin/agents` → `counts.total` |
| AG-2 | 목록: 태스크 에이전트(태스크 담당 → 팀 supervisor 바로 뒤에 그 멤버 → 나머지 task 에이전트) 먼저, 관리 에이전트(assistant·censor) 마지막. 행 = 이니셜 타일·`name`(1줄)·`subtitle`(「{태스크} 태스크」 / 「{supervisor} 팀 멤버 · 역할」 / 관리 역할)·`sandboxLine`(「rfa-main 샌드박스」)·상태 pill·「오늘 N회」 | `GET /admin/agents` |
| AG-3 | 관리 에이전트는 바꿀 수 없음: `editable=false`, `readOnlyReason` 「데모에서는 관리 에이전트를 수정할 수 없습니다.」 → 버튼 비활성 + 힌트 | 같은 응답, 쓰기 API 는 403 `management_agent` |
| AG-4 | 「추가」 → 태스크 추가 대화상자(1차 `POST /tasks`) | — |
| AG-5 | 상세 헤더: 이름·설명·상태, 「{sandbox} 샌드박스 설정 보기」 링크 | `GET /admin/agents/{id}` |
| AG-6 | 컨텍스트: 「지금 쓰는 양 {used} / {limit} 토큰」 + 시스템 프롬프트·도구 정의·기억과 노트·대화 기록·남은 공간 막대. 마지막 LLM 호출의 프록시 실측(`measured=true` 면 합계가 업스트림 usage, 비율은 추정). 호출 전이면 `context=null` | 같은 응답 `context` |
| AG-7 | 불러오는 자료: 사내 지식 API 의 자료마다 스위치, 사내 지식 경로가 없는 구획이면 「… 불러올 수 없음」 | `sources[]`, `PATCH /admin/agents/{id}/sources/{sourceId}` |
| AG-8 | 「대화 압축」(최근 세션만 남김)·「기억 비우기」(세션 + MEMORY.md·memory/) | `POST /admin/agents/{id}/compact`, `/clear-memory` |
| AG-9 | 「시스템 프롬프트 편집」: IDENTITY.md(서명 마커 가림)·스킬 보기, 운영자 추가 지시 저장 | `GET/PUT /admin/agents/{id}/prompt` |
| AG-10 | 호출 통계(오늘 · {제공자} · {모델}): 추론 호출·쓴 토큰(천)·평균 응답(초)·차단된 호출 + 최근 7일 막대 | `stats` |

### 샌드박스 페이지

| ID | 요구사항 | API |
|---|---|---|
| SB-1 | 사이드바 「샌드박스 N」, 목록: 이름·보안 레벨(`securityLabel`, 예 「L2 control-plane+intranet-ro」)·연결된 태스크·「{제공자} · 게이트웨이 {port}」·상태·「에이전트 N」 | `GET /admin/sandboxes` |
| SB-2 | 「추가」 → 「샌드박스는 많은 양의 메모리를 요구합니다. 현재 데모에서는 2개까지만 제공드립니다.」 | `limit`, `canAdd`, `limitMessage`; `POST /admin/sandboxes` 는 409 `sandbox_limit` |
| SB-3 | 상세 헤더: 보안 그룹, 태스크, 에이전트 명단 파일, 「이 샌드박스의 에이전트 N개 보기」 | `GET /admin/sandboxes/{id}` |
| SB-4 | LLM 추론: 제공자 라디오(NVIDIA / Gemini, 「Ollama 로컬」은 항상 선택 불가 「로컬 LLM을 확인할 수 없습니다.」), 모델, 컨텍스트 길이, 최대 응답 길이 | `inference` |
| SB-5 | 게이트웨이: 포트, 등록된 제공자 `llm-api`, 키 등록 여부, 「지금 추론 경로 llm-api / {model}」, 같은 경로를 쓰는 다른 샌드박스 | `gateway` |
| SB-6 | 「변경 적용」: 제공자·모델은 바로 적용(프록시 한 곳이라 모든 샌드박스 공통), 컨텍스트·최대 응답 길이는 저장 후 「다시 만들 때 적용」 | `PATCH /admin/sandboxes/{id}` → `{applied, requiresRecreate, sandbox}` |

권한: 조회는 모두, 쓰기는 owner(bearer) 만 — guest 403 `owner_only` 「게스트는 바꿀 수 없습니다. 소유자에게 요청하세요.」

## 2. 구현 메모

- 상태(`running|stopped|applying`)는 `nemoclaw list --json` + 샌드박스별 `agents list --json` 을 백그라운드에서 60초마다 갱신한 스냅샷(호스트 락 대기로 요청이 멈추지 않게). 첫 스냅샷 전에는 선언 기준이고 `observed=false`.
- 숫자는 감사 기록 `inference` 행(LLM 호출 1회 = 1행, 에이전트는 서명 마커로 식별). 프록시가 호출마다 `detail.context`(부분별 토큰)·`detail.usage` 를 남긴다(`nemoclaw/usage.py`).
- 자료 켜기/끄기와 추가 지시는 IDENTITY.md 에 섹션으로 붙여 다시 올린다(`nemoclaw <sb> upload`, 다음 대화부터). 지시 수준 적용이며, 부트스트랩·팀 생성이 IDENTITY 를 다시 시드하면 덮인다.
- 대화 압축·기억 비우기: 샌드박스 안 OpenClaw 세션 저장소(`agents/<id>/sessions/<sid>.jsonl` + `.trajectory*` + `sessions.json` 색인)를 sh(+node 색인 정리)로 정리한다.
- 제공자 전환: 프록시·검열 판정기·채팅 순위가 공유하는 `Routing`·`backend_keys` 를 제자리에서 바꾸고 `.env.dev` 의 `RFA_LLM_PROVIDER`·모델 키만 고친다(다른 줄·파일 권한 유지).
- 컨텍스트 길이 선택지는 8192/16384/32768/65536(기본 32768), 최대 응답 2048/4096/8192(기본 4096). 저장만 하며 실제 모델 호출에는 아직 쓰이지 않는다.
- 시안과 다른 점: 등급 4단 샌드박스 대신 실제 2개(D-16), 관리 에이전트 이름은 `assistant`·`censor`(부제로 「통합 질의 담당 …」「공개 범위 기밀 검토」), 네트워크 정책 섹션은 시안에서도 숨겨져 있어 스위치 API 없음(보안 그룹 정보만 읽기 전용 제공).
