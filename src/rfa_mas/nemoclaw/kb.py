"""Seed the local knowledge base the intranet API (knowledge facade) reads from.

Notes come from ``deploy/nemoclaw/kb/sg_kb_seed.jsonl`` (synthetic, public audience so the
facade may share them). Idempotent: an unchanged note is skipped and a note whose seed text
changed gets a new revision on the same source, so ``make kb-seed`` can run on every bootstrap.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import Audience, DomainId, KnowledgeAcl, KnowledgeProvenance, KnowledgeWrite
from rfa_mas.settings import Settings

SEED_PATH = Path(__file__).resolve().parents[3] / "deploy" / "nemoclaw" / "kb" / "sg_kb_seed.jsonl"
NAMESPACE = "sg-demo"


def load_seed(path: Path = SEED_PATH) -> list[dict]:
    notes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            notes.append(json.loads(line))
    return notes


async def seed(path: Path = SEED_PATH, settings: Settings | None = None) -> dict:
    container = build_container(settings or Settings())
    await container.startup()
    try:
        principal = await container.repository.local_principal()
        existing = {}
        for domain in (DomainId.TRIV3, DomainId.QUANTIZATION_RESEARCH):
            for revision in await container.knowledge.list(principal, domain_id=domain):
                prov = revision.provenance
                if prov.provider == "note" and prov.namespace == NAMESPACE:
                    existing[prov.external_id] = revision
        written, updated, skipped = [], [], []
        for note in load_seed(path):
            key = note["key"]
            current = existing.get(key)
            if current is not None and (current.document.title, current.document.content) == (
                note["title"],
                note["content"],
            ):
                skipped.append(key)
                continue
            # Seed text changed since the last run: write a new revision on the same source.
            digest = hashlib.sha256(f"{note['title']}\n{note['content']}".encode()).hexdigest()[:16]
            request = KnowledgeWrite(
                domain_id=DomainId(note["domain_id"]),
                provenance=KnowledgeProvenance(provider="note", namespace=NAMESPACE, external_id=key),
                provider_revision="1" if current is None else digest,
                expected_revision=None if current is None else current.document.source_revision,
                title=note["title"],
                content=note["content"],
                acl=KnowledgeAcl(audience=Audience.PUBLIC),
                synthetic=True,
            )
            await container.knowledge.write(request, principal)
            (written if current is None else updated).append(key)
        return {
            "written": written,
            "updated": updated,
            "skipped": skipped,
            "database": container.settings.database_url,
        }
    finally:
        await container.shutdown()


def main() -> int:
    report = asyncio.run(seed())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
