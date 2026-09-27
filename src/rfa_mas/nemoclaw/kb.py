"""Seed the local knowledge base the intranet API (knowledge facade) reads from.

Notes come from ``deploy/nemoclaw/kb/sg_kb_seed.jsonl`` (synthetic, public audience so the
facade may share them). Idempotent: a note whose provenance external_id already exists is
skipped, so ``make kb-seed`` can run on every bootstrap.
"""

from __future__ import annotations

import asyncio
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
        existing: set[str] = set()
        for domain in (DomainId.TRIV3, DomainId.QUANTIZATION_RESEARCH):
            for revision in await container.knowledge.list(principal, domain_id=domain):
                prov = revision.provenance
                if prov.provider == "note" and prov.namespace == NAMESPACE:
                    existing.add(prov.external_id)
        written, skipped = [], []
        for note in load_seed(path):
            key = note["key"]
            if key in existing:
                skipped.append(key)
                continue
            request = KnowledgeWrite(
                domain_id=DomainId(note["domain_id"]),
                provenance=KnowledgeProvenance(provider="note", namespace=NAMESPACE, external_id=key),
                provider_revision="1",
                title=note["title"],
                content=note["content"],
                acl=KnowledgeAcl(audience=Audience.PUBLIC),
                synthetic=True,
            )
            await container.knowledge.write(request, principal)
            written.append(key)
        return {"written": written, "skipped": skipped, "database": container.settings.database_url}
    finally:
        await container.shutdown()


def main() -> int:
    report = asyncio.run(seed())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
