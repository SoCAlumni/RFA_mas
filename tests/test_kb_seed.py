"""make kb-seed: unchanged notes are skipped, edited seed text becomes a new revision."""

import asyncio
import json

from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import DomainId
from rfa_mas.nemoclaw.kb import SEED_PATH, load_seed, seed
from rfa_mas.settings import Settings


def _write(path, notes):
    path.write_text("\n".join(json.dumps(n, ensure_ascii=False) for n in notes) + "\n", encoding="utf-8")


def _titles(settings):
    async def run():
        container = build_container(settings)
        await container.startup()
        try:
            principal = await container.repository.local_principal()
            rows = await container.knowledge.list(principal, domain_id=DomainId.QUANTIZATION_RESEARCH)
            return {r.provenance.external_id: (r.document.title, r.document.content) for r in rows}
        finally:
            await container.shutdown()

    return asyncio.run(run())


def test_seed_updates_changed_notes_and_skips_unchanged(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'kb.db'}", trace_dir=tmp_path / "t")
    old = {"key": "k1", "domain_id": "quantization_research", "title": "[샘플] 메모", "content": "[합성 샘플 · 실측 아님]\n본문"}
    same = {"key": "k2", "domain_id": "quantization_research", "title": "계획", "content": "본문2"}
    seed_file = tmp_path / "seed.jsonl"

    _write(seed_file, [old, same])
    first = asyncio.run(seed(seed_file, settings))
    assert first["written"] == ["k1", "k2"]

    _write(seed_file, [{**old, "title": "메모", "content": "본문"}, same])
    second = asyncio.run(seed(seed_file, settings))
    assert (second["updated"], second["skipped"]) == (["k1"], ["k2"])
    assert _titles(settings)["k1"] == ("메모", "본문")

    third = asyncio.run(seed(seed_file, settings))
    assert third["skipped"] == ["k1", "k2"] and not third["updated"]


def test_shipped_seed_has_no_sample_markers():
    for note in load_seed(SEED_PATH):
        assert not note["title"].startswith("[샘플]"), note["key"]
        assert "합성 샘플" not in note["content"], note["key"]
