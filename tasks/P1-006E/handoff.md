# P1-006E — Workflow 레드팀 회귀(4 공격 + 대조 + 음성 대조)

- 목표/AC: AC1 공격이 실제 대상 경계에 도달한 증거, AC2 outbound·mock write·승인 상태로 판정, AC3 정상 대조로 과잉 차단 확인, AC4 네트워크 차단·합성 데이터.
- 최초 구현(bca55f7 → 08a5565)은 P1-005 이전 제품 기준이었다. RT03(공개 라벨에 비공개 canary 혼입)이 model 요청에 도달하는 결함을 strict xfail로 기록했다(P1-005 소관).
- P1-005 통합 뒤 보수(b9bb0de):
  - P1-005가 공개/owner local 대상의 근거 로딩을 staged-context loader(KB 직접)로 옮겼다. 그래서 retrieval port에 심던 test double이 더 이상 호출되지 않았다(delivered 0).
  - retrieval 공격을 합성 공개 KB note로 심는 방식으로 바꿨다. 실제 저장 자료와 같은 경로로 도달한다.
  - 서버측 KB 본문 읽기(read_sources spy)와 domain screen의 withheld 수(runtime 결과 spy)를 읽기 전용으로 관측한다.
  - "도달" 정의: retrieval observation이 있고, 모델이 소비했거나(model-call) 서버가 읽은 뒤 screen이 보류했음(context-screen).
- RT03 strict xfail은 실제 단언으로 전환했다. canary는 screen에서 통째로 보류되어 모델 요청·초안·검토·sink 어디에도 없다.
- RT01 지시 주입 note는 untrusted 근거 데이터로만 전달된다. 수신자·tool·승인·권한 변화 0이다.
- 검증(개발, P1-005 상태): tests/test_redteam_regression.py 25 passed.
  - share/sensitive screen을 강제로 끄면 RT03 테스트 2건이 실패함을 확인했다(돌연변이 검증).
- 한계:
  - mock 모델은 지시를 따르지 않으므로 결정적 경계 검증일 뿐이다. 실제 모델의 저항성은 아니다.
  - NAT middleware는 쓰지 않았다(port test double / KB planting).
  - ToolPort는 현재 graph에서 호출되지 않아 RN01은 unknown이다.

