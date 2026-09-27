# legacy/ — 보류된 코드·설정·문서 (2026-09-27)

사용자 지시로 **RAM 프로파일·admission queue·Redis 관련 지시는 전부 보류**한다. 저장소에서 찾은 관련물은 여기로 옮겼고,
현재 작업(프런트 연동 API)에서는 참조하지 않는다. `/ask` 는 동기(서버 타임아웃 180초)로 단순화했고 `202 queued` 경로는 제거했다.

| 항목 | 원위치 | 여기 | 비고 |
| --- | --- | --- | --- |
| admission queue 구현(`AskService` FIFO 워커, `202 {queued, position}`, `GET /ask/{request_id}` 폴링, `queue_full` refusal) | `src/rfa_mas/nemoclaw/ask_api.py` | `nemoclaw/ask_api_admission.py` | 원본 전체 사본. 현재 `ask_api.py` 는 동기 + request_id 캐시만 |
| `AdmissionConfig(max_inflight, max_queue, …)` | `src/rfa_mas/nemoclaw/config.py` | `config/admission_config.py` | 현재는 `ServerConfig(timeout_seconds, result_ttl_seconds)` |
| `admission:` 선언 | `deploy/nemoclaw/ask.yaml` | `config/ask.admission.yaml` | 현재는 `server:` |
| 데모 06 admission queue | `demo/06_admission_queue.py` | `demo/06_admission_queue.py` | `make demo` 목록에서 제외 |
| 큐 테스트 2개 | `tests/test_ask.py` | `tests/test_ask_admission.py` | pytest 수집 대상 아님 |

찾지 못한 것: RAM 프로파일(코드·설정에 없음 — `docs/evidence/nemoclaw.md` 의 하드웨어 요구사항 기록만 있고 그대로 둠), Redis(코드에 없음. Colima 의 `*-redis-1`
컨테이너는 다른 프로젝트의 Langfuse 스택이며 이 저장소와 무관).
