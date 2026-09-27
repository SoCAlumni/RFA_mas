# OPS-007 문서 영향 검토

- 목표: 문서만 변경된 통합 task를 전체 테스트 두 번씩 돌려야 했던 반복을 제거한다.
- 구현: `review-doc-change`는 coordinator/revision/이전 실제 integration binding을 검사한다.
  정확한 docs Markdown delta, 동일 code/config/spec/contract/global context만 허용한다.
  요구 문서·새 파일/삭제·미커밋 변경·활성 claim·실패 근거는 거절한다.
- 원본 실행 report·시각은 불변이다. 새 문서 검토에는 reviewer/reason/hash와 이전 integration,
  `tests_reexecuted=false`가 있다. 제품 테스트를 새로 실행했다고 주장하지 않는다.
- 개발 검증: 최초 관리 fixture 12 passed(31.24s), 그 뒤 활성 worker 거절 사례 추가.
  최종 13개는 task evidence의 실제 worker/target 실행 결과를 확인한다.
- 정적 검사: 변경 Python 파일 ruff 및 git diff --check 통과.
- 다음 행동: 통합 후 manifest delta와 문서 diff를 직접 검토해 해당 작업만 이 명령으로 보존.
  실제 소스가 바뀐 작업은 필수 AC 재검증을 계속한다.
