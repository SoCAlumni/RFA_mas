"""Loader for the pinned synthetic E2E fixture pack in fixtures/e2e.

The pack fixes inputs, expectations and thresholds from RFA_E2E_Test_Scenarios_10_ko.md
before any scenario runs. Loading or validating it says nothing about product behavior.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rfa_mas.contracts import KnowledgeExport, KnowledgeWrite, TrustedPrincipal

PACK = Path(__file__).resolve().parents[2] / "fixtures" / "e2e"
MANIFEST = "manifest.json"
EXPORT_KEYS = {"confluence": ("id", "version"), "github_issue": ("number", "revision")}

FILLER_TOPICS = (
    "회의실 예약",
    "사내 교육",
    "문서 템플릿",
    "빌드 파이프라인",
    "보안 점검",
    "주간 보고",
    "채용 절차",
    "사무용품",
    "출장 정산",
    "고객 문의 분류",
    "코드 리뷰 규칙",
    "배포 체크리스트",
    "장비 반납",
    "사내 공지",
    "온보딩",
)
FILLER_SENTENCES = (
    "{topic} 관련 담당자는 이번 주 안에 진행 상황을 공유하기로 했다.",
    "{topic} 절차는 기존 안내 문서와 크게 다르지 않다.",
    "팀원들은 {topic} 항목을 다음 회의 안건으로 올리자고 제안했다.",
    "{topic}에 대한 질문은 사내 게시판의 전용 스레드에 남긴다.",
    "지난달 {topic} 회고에서는 문서 위치를 한곳으로 모으자는 의견이 나왔다.",
    "{topic} 일정은 부서 사정에 따라 조정될 수 있다.",
    "SDK 문서 링크 정리는 {topic} 논의와 별개로 진행한다.",
    "benchmark 결과 해석은 이 메모의 범위가 아니며 {topic} 내용만 다룬다.",
    "{topic} 체크 항목은 담당자가 분기마다 다시 확인한다.",
    "지연이나 정확도 같은 성능 지표는 {topic} 논의에 포함하지 않는다.",
    "{topic} 변경 사항은 승인 후 사내 공지로 알린다.",
    "{topic} 관련 비용 처리는 재무 포털에서 확인한다.",
)


def read_json(name: str) -> Any:
    return json.loads((PACK / name).read_text(encoding="utf-8"))


def read_jsonl(name: str) -> list[Any]:
    lines = (PACK / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def file_digest(name: str) -> str:
    return hashlib.sha256((PACK / name).read_bytes()).hexdigest()


def pack_files() -> list[str]:
    return sorted(
        path.relative_to(PACK).as_posix()
        for path in PACK.rglob("*")
        if path.is_file() and path.name != MANIFEST
    )


def sources() -> dict[str, dict[str, Any]]:
    return {entry["fixture_id"]: entry for entry in read_json("sources.json")["sources"]}


def set_members(name: str) -> list[str]:
    return [fid for fid, entry in sources().items() if name in entry["sets"]]


def personas() -> dict[str, TrustedPrincipal]:
    return {
        item["persona_id"]: TrustedPrincipal.model_validate(item["principal"])
        for item in read_json("personas.json")["personas"]
    }


def golden() -> list[dict[str, Any]]:
    return read_jsonl("golden_questions.jsonl")


def _exports() -> list[dict[str, Any]]:
    files = read_json("sources.json")["files"]
    return [read_json(files[channel]) for channel in EXPORT_KEYS]


def payloads() -> dict[str, tuple[str, dict[str, Any]]]:
    """Map each fixture ID to (channel, raw note request or export row).

    Raises if an ingest payload has no index entry, or an index entry resolves to
    zero or several payloads, so expectations cannot silently drift from inputs.
    """
    index = sources()
    by_locator = {(entry["channel"], entry["external_id"]): fid for fid, entry in index.items()}
    found: dict[str, tuple[str, dict[str, Any]]] = {}

    def claim(fid: str, channel: str, payload: dict[str, Any]) -> None:
        if fid in found:
            raise ValueError(f"duplicate payload for {fid}")
        found[fid] = (channel, payload)

    for line in read_jsonl(read_json("sources.json")["files"]["note"]):
        request = line["request"]
        locator = ("note", request["provenance"]["external_id"])
        if by_locator.get(locator) != line["fixture_id"]:
            raise ValueError(f"note {locator} does not match index {line['fixture_id']}")
        claim(line["fixture_id"], "note", request)
    for batch in _exports():
        id_key, _ = EXPORT_KEYS[batch["provider"]]
        for row in batch["rows"]:
            locator = (batch["provider"], str(row[id_key]))
            if locator not in by_locator:
                raise ValueError(f"export row {locator} is not indexed")
            claim(by_locator[locator], batch["provider"], row)
    missing = set(index) - set(found)
    if missing:
        raise ValueError(f"indexed sources without payload: {sorted(missing)}")
    return found


def payload_revision(channel: str, payload: dict[str, Any]) -> str:
    if channel == "note":
        return payload["provider_revision"]
    return payload[EXPORT_KEYS[channel][1]]


def payload_text(channel: str, payload: dict[str, Any]) -> str:
    return payload["content"] if channel == "note" else payload["body"]


def note_writes(set_name: str | None = None) -> list[tuple[str, KnowledgeWrite]]:
    wanted = set(set_members(set_name)) if set_name else None
    return [
        (fid, KnowledgeWrite.model_validate(payload))
        for fid, (channel, payload) in payloads().items()
        if channel == "note" and (wanted is None or fid in wanted)
    ]


def export_batches(
    set_name: str | None = None, *, ids: Iterable[str] | None = None
) -> list[tuple[list[str], KnowledgeExport]]:
    wanted = set(set_members(set_name)) if set_name else None
    if ids is not None:
        chosen = set(ids)
        wanted = chosen if wanted is None else wanted & chosen
    index = sources()
    by_locator = {(entry["channel"], entry["external_id"]): fid for fid, entry in index.items()}
    result = []
    for batch in _exports():
        id_key, _ = EXPORT_KEYS[batch["provider"]]
        rows, ids = [], []
        for row in batch["rows"]:
            fid = by_locator[(batch["provider"], str(row[id_key]))]
            if wanted is None or fid in wanted:
                rows.append(row)
                ids.append(fid)
        if rows:
            result.append((ids, KnowledgeExport.model_validate(batch | {"rows": rows})))
    return result


def filler_payloads(spec: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Deterministic distractor notes for the 1,000-document retrieval load set.

    Selection uses SHA-256 of (seed, document, slot) so output does not depend on the
    Python random module implementation.
    """
    spec = spec or read_json("load_corpus.json")
    seed, low = spec["seed"], spec["document_chars"]["min"]

    def pick(index: int, slot: str, options: tuple[str, ...]) -> str:
        digest = hashlib.sha256(f"{seed}:{index}:{slot}".encode()).digest()
        return options[int.from_bytes(digest[:4], "big") % len(options)]

    result = []
    for index in range(spec["filler_count"]):
        topic = pick(index, "topic", FILLER_TOPICS)
        parts = [f"[합성 부하 문서 {index:04d}] {topic} 관련 일반 메모."]
        slot = 0
        while len(" ".join(parts)) < low:
            parts.append(pick(index, str(slot), FILLER_SENTENCES).format(topic=topic))
            slot += 1
        result.append(
            {
                "domain_id": "triv3",
                "provenance": {
                    "provider": "note",
                    "namespace": "triv-demo/load",
                    "external_id": f"load-{index:04d}",
                },
                "provider_revision": "1",
                "title": f"합성 부하 문서 {index:04d} - {topic}",
                "content": " ".join(parts),
                "acl": spec["acl"],
                "source_modified_at": "2026-09-20T09:00:00+09:00",
                "synthetic": True,
            }
        )
    return result


def filler_digest(items: list[dict[str, Any]]) -> str:
    lines = (json.dumps(item, ensure_ascii=False, sort_keys=True) for item in items)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()
