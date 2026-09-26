# P0-025A — 동일 출처 세션·노트·질의·검토 기본 UI

## 구현 사실 (wip/P0-025A 3adfe5e, P1-008D 위)

- src/rfa_mas/ui/app.py와 static(index.html/app.js/app.css): 세션 생성/목록, 노트 입력, 질의/결과, pending 검토와 수동 승인, 상태 새로고침. 고정 core/review 서비스 주입만, 사용자 URL proxy 없음, CSRF·Origin·Host 검사, 텍스트 렌더링(innerHTML/eval/storage 없음), 서비스 token은 HTML/JS/응답/URL/cookie에 없음. mock/local 표시.
- timeout은 outcome_unknown/query_required(자동 재시도 없음), 500 마스킹, 폐기 token은 upstream_auth_failed, 승인 변경은 409.

## 검증

- tests/test_local_ui.py 7 passed. 브라우저 실제 렌더링은 HTTP/정적 분석으로만 확인했다. 실제 UI gate(다영)는 not_run.

## 다음

- bootstrap/CLI local-stack 조립은 P0-025/P1-008.

