# P0-027 — NAT compatibility spike 시작점

목표/AC: 설치 후보의 실제 resolve/import/등록·custom state 변환 가능성 및 지원 범위를 확인한다(AC1~AC4). task 상태 원본은 task.yaml이다.

관찰 사실(2026-09-26): Python 3.12.13, LangGraph 1.2.12, checkpoint 4.2.0. NAT/nvidia-nat-langchain/Langfuse/opentelemetry-api는 설치되지 않았다. Settings는 local trace와 reserved Langfuse/Judge 선택을 가진다. LocalJsonlTrace가 실제 구현이며 두 번째 관측 서버는 필요 없다.

관련 소스: pyproject.toml/uv.lock, application/service.py의 WorkService, graphs/domain.py의 DomainState(work/task/principal), ports/interfaces.py. 현재 대표 graph에 ToolPort 실행은 연결되지 않았고, graph_checkpoints는 재개용 LangGraph saver가 아니다. 새 checkpoint 작업을 이 spike에서 구현하지 않는다.

조사와 추정 구분: 공식 latest 문서가 1.8로 표시되고 wrapper의 message/CompiledStateGraph 조건을 확인했다. develop metadata의 Python/LangGraph 범위에 현재 pin이 들어가지만 실제 release 조합의 resolve 성공·동작은 **미검증**이다. `docs/EVALUATION_CONTEXT.md B`에 공식 링크와 제한을 모았다.

결정: core pin과 WorkService 경계를 유지한 optional adapter. actual release 선택·격리 venv smoke·최소 extra/lock은 이 task에서 coordinator가 직렬 관리한다. streaming/HITL resume/내부 tool 자동 관측은 P0 지원 주장 밖이다. 원문 없는 safe local trace를 사용할 수 있는지 검증한다. 설치 실패가 KB/평가의 의존성이 되지 않게 한다.

검증/시도: metadata/소스/공식 문서 조회만 했다. 설치·NAT smoke·프로파일링·제품 AC·보안 실험은 not_run이다. 실패한 NAT 실행을 숨기거나 fake runner 성공으로 대체하지 않는다.

다음 첫 행동: OPS-000 baseline과 taskctl 최신 revision/claim 확인 → pyproject/lock 비교 → 공개 release tag와 package constraint를 선택 → 임시 venv에서 resolve/import, planned tests/spikes/test_nat_compatibility.py로 custom input smoke. 실제 argv/version/result를 evidence에 남기고 성공 시 P0-028에 입력 mapping·지원/미수집 범위·extra 버전을 전달한다. 범위를 0.5h 안에 해결하지 못하면 blocker/다음 접근을 남기고 core 전체를 재작성하지 않는다.
