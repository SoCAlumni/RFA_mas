# P1-008M — NemoClaw 운영 채널

## 구현 사실 (210695d, 8f076a9)
- src/rfa_mas/poc/channel.py: ChannelGateway(bearer key 파일, GET /healthz·POST /channel/chat만, 그 외 403, 키 없음 401), /channel/chat은 UI와 같은 LocalChat.send 파이프라인(LLM 추론·Task 팀·KB·단계) 실행, 응답에 reply/route/stages/answer_model/team. healthz는 forwarded 헤더 제거·loopback 재작성 후 코어에 전달.
- PoC --channel-bind IP:PORT(비loopback만)·--channel-key-file(기본 <data-dir>/channel-api.key, 0600 생성). 같은 uvicorn 서버가 두 socket을 서비스하고 listener 포트로 UI/채널을 분기.
- deploy/nemoclaw: rfa-assistant.yaml(preset 템플릿), skills/rfa-assistant/SKILL.md(OpenClaw skill), setup.sh(dry-run→apply, 헤더/env 업로드, chmod 600, skill install), ask.sh(agent 턴).
- docs/POC.md·docs/evidence/nemoclaw.md §8B.

## 검증
- tests/test_poc_channel.py 4개: key 파일 0600/재사용/빈 파일 거절, allowlist(assistant/sessions/runs/knowledge 거부), 인증·forwarded 제거·loopback, PoC 두 listener 분기 + /channel/chat 저장/질의 + UI history 동일 세션.
- 실제 sandbox(rfa-demo): probe 허용/거부 OCSF 일치, /channel/chat 실제 모델 답변, OpenClaw agent 턴이 Research Task 팀 결과(Supervisor LLM 요약) 보고, UI 최근 대화에 표시. .agent/evidence/P1-008M/live/observation.json.
- 관찰: agent가 같은 메시지로 채널을 2회 호출(세션 2개). 후속: skill에 message_id 멱등 지정.

## 다음 행동
integrate → close → 사용자 8780을 main 코드로 --channel-bind 포함 재시작(setup 재실행 불필요: key 파일 동일).
