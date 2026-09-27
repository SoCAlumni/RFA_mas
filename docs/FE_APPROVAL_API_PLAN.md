# 결재 프로세스 API (RFA_module 연동)

- 기준: 시안 4차 판(artifact `LFD3Utajj2ahC9wv45MbAg`, `1790530327-a276`)의 결재함·결재 상세·초안 카드·「소스」 패널, RFA_module(`../RFA_module`) v0.2.0 계약(`contracts/head.openapi.yaml`, `contracts/approvals.openapi.yaml`, `docs/agent_api.md`).
- 상태: 결정(D-20~D-24, `docs/decisions.md`)·구현·라이브 전환 완료(WORK_LOG FE-7). 계약 원본은 `docs/openapi.yaml`(tag `head`, `inbox`).

## 1. 지금 RFA_module 이 하는 일

```
GitHub/Slack 멘션 ─▶ desk(대응 에이전트, 5초 폴링)
   1) POST {HEAD_URL}/ask            ← 이 시스템(rfa_mas)이 받을 요청. 지금은 head_stub(:8791/8792)
   2) writer LLM 이 답 초안 작성       (RFA_module 안)
   3) POST {APPROVALS_URL}/approvals  ← RFA_module 결재 서버(:8790): pending, round 1
사람 ─▶ POST /approvals/{id}/approve  → 결재 서버가 GitHub/Slack 에 게시 → posted
     └▶ POST /approvals/{id}/reject {reason} → desk 가 feedback=거절 이력으로 1) 다시 → revise → pending, round+1
         (3번째 거절이면 closed = 「3번 거절되어 응답하지 않기로 했어요」)
```

- 게시(publish)는 RFA_module 결재 서버만 한다. 콜백·SSE 없음(전부 폴링). 결재 API 에 인증 없음.
- 등급: `audience` 가 채널로 고정(GitHub=public=사외, Slack=company=사내) → D-0.4(요청 단위 등급)와 맞다.
- **지금 `HEAD_URL` 을 rfa_mas `/ask` 로 바꾸면 깨진다**: rfa_mas 는 bearer·`request_id` 필수, `refusal` 이 객체(RFA_module 은 문자열). RFA_module 에는 토큰 설정도 없다.

## 2. 요구사항

### 2.1 RFA_module 이 보내는 요청 받기

| ID | 요구사항 |
|---|---|
| H-1 | RFA_module head 계약(v0.2.0) 그대로 받는다: 요청 `{question, channel, audience, target, url, requester, context[], feedback[]}`, 응답 `{knowledge, task{id,name}\|null, refusal: string\|null}`. RFA_module 은 `.env` 의 `HEAD_URL` 한 줄만 바꾼다. |
| H-2 | 재시도에 안전: RFA_module 은 `request_id` 를 안 보내고 타임아웃(120초) 뒤 최대 3번 다시 보낸다 → 서버가 `url`+차수(round = 거절 수 + 1)로 멱등키를 만들어 같은 파이프라인 결과를 돌려준다. 서버 처리 한도는 120초 안(110초). |
| H-3 | 크기 제한 초과(긴 이슈 본문 등)는 422 대신 잘라서 처리한다. |
| H-4 | 요청이 들어오는 즉시 결재함에 「작성 중」 항목이 생기고 단계(RAG 검색 → 검증)가 실시간으로 진행된다. desk 가 결재를 올리면 같은 항목이 「결재 필요」가 된다. |
| H-5 | 요청의 등급·담당 태스크: 등록된 소스(§2.4)에서 온 요청은 그 소스의 기본 등급과 태스크를 쓰고, 아니면 RFA_module 이 보낸 audience 와 head 라우팅을 쓴다. |

### 2.2 결재함 목록

| ID | 요구사항 |
|---|---|
| L-1 | 행: 요청자·이니셜, 담당 에이전트 태그(desk 이름·색·아이콘, 「{에이전트} 담당」), 제목, 등급 배지(사외/사내), 상태 줄, 상태 배지, 시각, 채널. |
| L-2 | 상태 줄·배지: 작성 중 「답변 초안 작성 중」/ pending 「결재 {id} · 답변 초안 승인 대기」(주입 의심이면 「결재 {id} · 주입 문장 제외 · 답변 초안 승인 대기」 + 배지 「차단됨」, 아니면 「결재 필요」) / rejected 「재생성 중」 / approved 「보내는 중」(게시 실패면 「게시 실패」) / posted 「결재 {id} · 응답함」+「응답 완료」 / closed 「결재 {id} · 응답하지 않기로 결정」+「결재 완료」. |
| L-3 | 필터: 전체·태스크별(`#inbox/{taskId}`), 상태별(결재 필요). 개수: 결재함 배지·「결재 필요 N」·태스크별 = pending 수. 좌측 태스크·대화 상대의 「맡은 안건 N건」(`itemCount`)도 이 값. |
| L-4 | 제목: GitHub 은 이슈 제목, 그 밖에는 head 가 만든 질의 요약, 없으면 질문 첫 문장. |

### 2.3 결재 상세 · 초안 카드

| ID | 요구사항 |
|---|---|
| D-1 | 경로 줄 「{태스크} / {대상}」, 제목, 상태 pill, 요청자, 「GitHub 이슈로 들어옴 · {시각}」/「Slack 다이렉트 메시지로 들어옴 · {시각}」. |
| D-2 | 원본 화면: GitHub 은 `owner/repo`·이슈 번호·제목·본문·댓글(작성자·시각), Slack 은 대화 메시지(작성자·시각), 원본 링크(`url`). |
| D-3 | 주입 의심 문장 표시(「표시한 문장은 질문이 아니라 에이전트에게 내리는 지시로 보입니다.」) — 문장 단위 위치. |
| D-4 | 초안 카드: 결재 번호, 담당 에이전트, 등급, 단계 3개(RAG 검색 · 검증 · LLM 초안: 상태·걸린 시간·요약·세부 항목(tone ok/warn/block)), 재생성 횟수와 마지막 재생성 요청, 초안 본문, 글자 수, 게시 결과(`postedUrl`). |
| D-5 | 「바로 응답」= 승인·게시(RFA_module approve). 게시 실패면 다시 시도 가능. |
| D-6 | 「재생성 요청」(최대 400자, 빠른 문구 칩) = RFA_module reject(사유 = 요청 내용) → desk 가 다시 써서 round+1. 3번째 요청이면 닫힘(응답하지 않기). |
| D-7 | 초안을 고쳐서 보내기 — RFA_module 에 초안 수정 API 가 없음(§4 D-22). |

단계의 실제 출처: RAG 검색 = head 라우팅(태스크·에이전트) + 담당 에이전트 답의 근거 인용, 검증 = 검열 판정(등급·제외 항목)·주입 의심·차단된 시도, LLM 초안 = RFA_module desk 가 쓴 초안(글자 수, 걸린 시간 = 검증 끝 → 결재 등록). 문서 점수·토큰 수는 rfa_mas 가 알 수 없어 뺀다.

### 2.4 소스 (관리 · 에이전트 상세의 「소스」 패널)

| ID | 요구사항 |
|---|---|
| S-1 | 태스크 에이전트마다 소스 목록: 종류(GitHub/Slack)·대상·받을 범위·기본 등급·상태(연결됨)·최근 요청 시각. 관리 에이전트에는 없음. |
| S-2 | 추가: GitHub 「owner/repo」, Slack 「#채널」또는 「DM · 팀이름」; 받을 범위(GitHub 이슈·PR 코멘트·Discussions / Slack 멘션·모든 메시지·DM) 1개 이상; 기본 등급(사외/사내). 오류 문구는 시안 그대로. 같은 태스크 중복은 409. |
| S-3 | 떼기. 쓰기는 owner 만. |
| S-4 | 들어오는 요청과 연결: GitHub 은 `target` 의 저장소, Slack 은 채널 id(`#C0123ABC`) 또는 DM 상대 이름으로 맞춘다. 맞는 소스가 있으면 그 등급·태스크(H-5). |
| S-5 | 받을 범위는 RFA_module 이 실제로 무엇을 폴링할지 정하지 못한다(RFA_module 은 `.env` 의 저장소 목록을 본다) → 표시·기록용. |

### 2.5 이번에 하지 않는 것

시안에서도 실행 시 지워진 것: 공개 범위 선택지(「어디까지 공개할까요?」), 「응답하지 않기」 버튼, 규칙 저장·자동응답, 작성자 차단. RFA_module 에 해당 API 가 없다. 목록 탭 「자동응답」은 0.

## 3. API

RFA_module 이 부르는 것:

| API | 내용 |
|---|---|
| `POST /v1/head/ask` | RFA_module head 계약 v0.2.0 호환(H-1~H-5). `HEAD_URL=http://127.0.0.1:8799/v1/head` |

프런트가 부르는 것(rfa_mas → RFA_module 결재 서버 프록시 + 보강):

| API | 내용 |
|---|---|
| `GET /inbox?task=&status=` | 결재함 목록(작성 중 항목 포함). 항목 id 는 원본 URL 기준으로 고정(작성 중 → 결재 등록 후에도 같은 id) |
| `GET /inbox/summary` | 결재 필요 수(전체·태스크별), 상태별 수 |
| `GET /inbox/{itemId}` | 상세 + 원본 + 초안 카드(단계·이력) |
| `POST /inbox/{itemId}/respond` | 바로 응답(승인·게시). owner |
| `POST /inbox/{itemId}/regenerate` `{request}` | 재생성 요청. owner |
| `GET /tasks/{taskId}/sources` | 소스 목록 |
| `POST /tasks/{taskId}/sources` `{kind, target, scopes[], grade}` | 소스 연결. owner |
| `DELETE /tasks/{taskId}/sources/{sourceId}` | 소스 떼기. owner |

기존 확장: `GET /tasks`·`GET /agents` 의 `itemCount` = 결재 필요 수.

## 4. 결정 (2026-09-28)

- D-20 결재 상태의 주인 = RFA_module 결재 서버 유지, rfa_mas 는 프록시·보강·owner 권한
- D-21 `POST /v1/head/ask` 호환 엔드포인트(인증 없음·루프백만, 멱등키 = url+차수, refusal 문자열, 110초)
- D-22 초안 수정은 우선 불가(409 `draft_edit_unsupported`) — RFA_module 제안서 `docs/proposals/RFA_MODULE_DRAFT_EDIT.md`
- D-23 게스트는 사외 항목만(목록·상세), 쓰기는 owner
- D-24 등록된 소스의 기본 등급·태스크가 채널 기본값보다 우선
- 라이브 전환: RFA_module `.env` `HEAD_URL=http://127.0.0.1:8799/v1/head`, desk 재시작

## 5. 구현 메모

- 항목 상태: `drafting`(head 가 받음, 결재 전) → `stalled`(head 답 뒤 10분 동안 결재가 안 올라옴, 「초안 없음」) / `pending` → `regenerating` → `pending`(round+1) / `posting` → `posted` / `publish_failed` / `closed`.
- OpenClaw 실패 문구(「LLM request failed.」「No response from OpenClaw.」)는 지식이 아니라 담당 에이전트 실패 → refusal 「답할 근거를 찾지 못했습니다」(desk 는 정중한 답 불가 초안을 쓴다).
- 단계: RAG 검색 = head 라우팅(또는 소스 지정) + 담당 에이전트 답의 인용 id, 검증 = 검열 판정 규칙(한국어 이름)·주입 의심 문장·이 요청 세션에서 막힌 시도, LLM 초안 = RFA_module desk(글자 수, 검증 끝 → 결재 등록 시간). head_stub 시절 결재는 rfa_mas 기록이 없어 앞 두 단계가 `unknown`.
- 소스의 Slack 채널 이름(`#infer-research`)은 채널 id 를 모르면 맞출 수 없다 → 채널 id 형태(`#C0123ABC`)나 DM 상대 이름으로 등록.
