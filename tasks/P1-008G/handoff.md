# P1-008G — 채팅 중심 PoC와 내 KB

## 구현 사실 / AC

- `70b45c8`, `bd8921a`: 기존 폼 UI를 비서 채팅/내 KB/승인함으로 교체. PC와390px 모바일 대응.
- AC1: `application/chat.py`의 교체형 ChatPort/ChatMessage/결정적 분류. 명시 메모·정보형 문장→비공개 저장, 질문→private core graph 검색, 공개 초안→public 근거 경로. 모호한 요청은 안내. 브라우저가 권한이나 intent를 임의 제출하지 않는다.
- AC2: `poc/chat.py`는 fixed core HTTP consumer + 별도 chat/history.db. 사용자 입력/근거·run 참조만 보관, session/message key의 durable 중복·본문 충돌 검사, 중단 pending→outcome_unknown. 다시 실행하지 않는다. 최근 대화 제목·원문 KB 필터/조회, 기존 폼의 세션 대화도 현재 core presentation으로 표시.
- AC3: 대화 session과 질문별 LangGraph execution session을 분리. 기존 thread_busy/승인 검사를 완화하지 않는다. 개인 답변도 core의 검토 대상 초안이며 상태는 실행정보에 유지. 공개 초안만 승인함에 표시하고 기존 수동 승인·core resume·모의 게시 경계를 재사용한다. 표시할 때마다 현재 ACL 검증; 과거 답변 캐시 없음.
- AC4: Chrome154 실제 새 프로필에서 저장/검색/KB/새로고침/수동 승인/mock receipt/XSS 문자열/390px 레이아웃을 확인. 브라우저 저장소·외부 outbound·JS 오류 없음. 상단과 입력창에 local/mock·규칙 기반 표시.

## 변경 파일 / 교체 경계

`src/rfa_mas/application/chat.py`, `src/rfa_mas/poc/chat.py`, `poc/bootstrap.py`, `ui/app.py`, `ui/static/{index.html,app.css,app.js}`, `tests/test_chat_poc.py`, `docs/POC.md`.
공통 DTO·core graph·기존 DB/migration·settings·lockfile 변경 없음. ChatPort 구현과 bootstrap만 교체 가능.

## 검증 / 실패

- 첫 개발 pytest:26pass/2fail. 승인대기 thread에서 다음 질문 거절→대화/실행 분리; 기존 mode metadata 누락→명시 local/not_run 보존.
- 두 번째 개발 pytest:28pass. 이후 일반 이름 문장·기존 폼 대화 보존 추가.
- 정식 worker01:30pass/0fail/skip + 실제 Chrome 통과. 화면 점검에서 개인 답변 요약 조건이 draft adapter가 아니라 model provenance를 확인해야 함을 발견해 bd8921a로 보수. worker02는 새 source에 연결한다.
- worker02 command30pass; 브라우저가 재사용 fixture의 이전 게시 카드를 고르는 오류로 publication_exists(정상 중복 차단). `6d73846`에서 카드의 draft ID를 노출하고 검사도 승인한 정확한 ID의 영수증을 관찰하도록 수정했다. 실패 evidence를 보존하며 세 번째 worker가 최종 검증이다. 핵심 AC·승인 검사는 완화하지 않았다.
- worker03: source6d73846, command30passed/0fail/skip; fresh Chrome154의 전 항목 통과. 실제 UI 화면도 시각 확인했다. 추가 worker 검증 없이 제출한다.
- lint/format, JS syntax, diff check 통과. 기존 내부 subgraph durability warning은 숨기지 않았다.
- evidence: `.agent/evidence/P1-008G/`; 개발 실패 요약 `development/errors.json`. 원래 개발실패 소스 manifest는 미캡처임을 명시했다.
- 상태 도구1회 root누락, 타 세션 P1-010 작성 중 참조 검사 거절; 기존 파일 보존, 명시root helper/후속 heartbeat 성공. 동일 실패 반복 루프 없음.

## 한계 / 다음 행동

- NVIDIA·실제 게시·OpenShell·팀원 UI real gate 검증 아님. 자유 대명사 해석/LLM 의도분류는 미구현. 채팅은 저장/검색/공개초안 범위; Task팀/스케줄 UX 없음.
- 개인 검색 응답을 보여주는 것이 run 완료나 게시 승인이 아니다. 원문 응답·근거·실제 상태는 상세에서 확인한다.
- 새 상태 결과는 task.yaml/evidence가 원본. worker02 제출 후 main 통합·동일 scoped30검증+브라우저 확인, 8780 사용자 서버를 기존 data-dir로 재시작한다. 다른 세션의 P1-010/P1-011 파일은 건드리지 않는다.
