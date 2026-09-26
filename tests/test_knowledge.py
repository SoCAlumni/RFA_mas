from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from rfa_mas.adapters.local import SqliteWorkRepository
from rfa_mas.api.app import create_app, resolve_principal
from rfa_mas.bootstrap import build_container
from rfa_mas.contracts import (
    Audience,
    KnowledgeDelete,
    KnowledgeDocument,
    KnowledgeExport,
    KnowledgeImportRow,
    KnowledgeWrite,
    TrustedPrincipal,
)
from rfa_mas.errors import RfaError
from rfa_mas.settings import Settings

OWNER = TrustedPrincipal(
    user_id="kb-owner",
    authenticated=True,
    company_id="company-a",
    business_units=frozenset({"project-a"}),
)
OTHER = TrustedPrincipal(user_id="kb-other", authenticated=True)


def note(**changes):
    data = dict(
        domain_id="triv3",
        provenance={"provider": "note", "namespace": "local-notes", "external_id": "note-1"},
        provider_revision="opaque-z",
        title="  note title  ",
        content="  original body\n\n",
        synthetic=True,
    )
    return KnowledgeWrite.model_validate(data | changes)


async def test_note_crud_raw_text_private_acl_history_and_delete(container):
    original = note()
    first = await container.knowledge.write(original, OWNER)
    doc = first.document
    assert doc.content == "  original body\n\n" and doc.title == "  note title  "
    assert doc.audience == Audience.PRIVATE and doc.owner_id == OWNER.user_id
    assert doc.source_id != original.provenance.external_id
    assert first.provider_revision == "opaque-z" and first.revision_number == 1
    assert await container.knowledge.get(doc.source_id, OWNER) == first
    assert await container.knowledge.list(OWNER) == [first]
    changed = note(
        provider_revision="opaque-a",
        expected_revision=doc.source_revision,
        content=" next revision ",
        acl={"audience": "public"},
    )
    second = await container.knowledge.write(changed, OWNER, source_id=doc.source_id)
    assert second.revision_number == 2 and second.document.source_revision != doc.source_revision
    assert second.acl_revision == second.document.source_revision != first.acl_revision
    assert (
        await container.knowledge.get(doc.source_id, OWNER, revision=doc.source_revision) == first
    )
    assert await container.knowledge.write(original, OWNER) == first
    assert (
        await container.knowledge.get(doc.source_id, OWNER) == second
    )  # Replay cannot rewind head.
    visible = await container.repository.list_documents("triv3")
    assert [d for d in visible if d.source_id == doc.source_id] == [second.document]
    deletion = KnowledgeDelete(
        expected_revision=second.document.source_revision, mutation_id="delete-one"
    )
    deleted = await container.knowledge.delete(doc.source_id, deletion, OWNER)
    assert deleted.deleted and deleted.revision_number == 3
    assert await container.knowledge.delete(doc.source_id, deletion, OWNER) == deleted
    assert await container.knowledge.write(original, OWNER) == first
    assert await container.knowledge.list(OWNER) == []
    assert not [
        d
        for d in await container.repository.list_documents("triv3")
        if d.source_id == doc.source_id
    ]
    with pytest.raises(RfaError, match="source"):
        await container.knowledge.get(doc.source_id, OWNER)
    assert (
        await container.knowledge.get(doc.source_id, OWNER, revision=doc.source_revision) == first
    )
    with sqlite3.connect(container.repository.path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM kb_documents WHERE source_id=?", (doc.source_id,)
            ).fetchone()[0]
            == 3
        )


async def test_provenance_and_owner_separation_conflict_and_cas(container):
    first = await container.knowledge.write(note(), OWNER)
    foreign = await container.knowledge.write(note(), OTHER)
    provenance = note().provenance.model_copy(update={"namespace": "different"})
    same_content = await container.knowledge.write(note(provenance=provenance), OWNER)
    assert len({r.document.source_id for r in (first, foreign, same_content)}) == 3
    with pytest.raises(RfaError) as error:
        await container.knowledge.write(note(content="changed immutable revision"), OWNER)
    assert error.value.code == "idempotency_conflict"
    other_repo = SqliteWorkRepository(container.repository.path)
    writes = [
        note(provider_revision=name, expected_revision=first.document.source_revision, content=name)
        for name in ("cas-left", "cas-right")
    ]
    outcomes = await asyncio.gather(
        *[
            repo.write_knowledge(request, OWNER, policy_version="local-v1")
            for repo, request in zip((container.repository, other_repo), writes, strict=True)
        ],
        return_exceptions=True,
    )
    assert sum(not isinstance(x, Exception) for x in outcomes) == 1
    assert [x.code for x in outcomes if isinstance(x, RfaError)] == ["idempotency_conflict"]
    current = await container.knowledge.get(first.document.source_id, OWNER)
    assert current.revision_number == 2
    with pytest.raises(RfaError) as error:
        await container.knowledge.write(note(provider_revision="without-cas"), OWNER)
    assert error.value.code == "idempotency_conflict"
    await container.knowledge.write(note(), OWNER)
    assert await container.knowledge.get(first.document.source_id, OWNER) == current


@pytest.mark.parametrize(
    "scope",
    [
        {"audience": "company", "company_id": "company-b"},
        {"audience": "business_unit", "company_id": "company-a", "memberships": ["other-project"]},
    ],
)
async def test_org_sharing_requires_current_server_membership(container, scope):
    with pytest.raises(RfaError) as error:
        await container.knowledge.write(note(acl=scope), OWNER)
    assert error.value.code == "policy_denied"
    assert await container.knowledge.list(OWNER) == []


async def test_org_sharing_can_only_use_real_fixture_membership(container):
    request = note(
        acl={"audience": "business_unit", "company_id": "company-a", "memberships": ["project-a"]}
    )
    result = await container.knowledge.write(request, OWNER)
    assert result.document.required_memberships == ("project-a",)
    revoked = OWNER.model_copy(update={"business_units": frozenset()})
    with pytest.raises(RfaError) as error:
        await container.knowledge.write(request, revoked)
    assert error.value.code == "policy_denied"


@pytest.mark.parametrize(
    "bad",
    [
        OWNER.model_copy(update={"authenticated": False}),
        OWNER.model_copy(update={"authenticated": "yes"}),
    ],
)
async def test_mutated_or_unauthenticated_principal_not_authority(container, bad):
    with pytest.raises(RfaError) as error:
        await container.knowledge.write(note(), bad)
    assert error.value.code == "authentication_required"


async def test_foreign_and_missing_sources_are_indistinguishable(container):
    own = await container.knowledge.write(note(), OWNER)
    for source_id in (own.document.source_id, "absent-source"):
        calls = [
            container.knowledge.get(source_id, OTHER),
            container.knowledge.write(note(), OTHER, source_id=source_id),
            container.knowledge.delete(
                source_id,
                KnowledgeDelete(
                    expected_revision=own.document.source_revision, mutation_id="delete"
                ),
                OTHER,
            ),
        ]
        for call in calls:
            with pytest.raises(RfaError) as error:
                await call
            assert error.value.code == "not_found"
    assert await container.knowledge.list(OTHER) == []


@pytest.mark.parametrize("fixture", ["github_issues.json", "confluence_pages.json"])
async def test_two_export_fixtures_partial_receipts_replay_and_repair(container, fixture):
    path = Path(__file__).parents[1] / "fixtures/imports" / fixture
    batch = KnowledgeExport.model_validate_json(path.read_text())
    bad_row = dict(batch.rows[0]) | {"owner_id": "PRIVATE_EXTRA_KEY_CANARY"}
    mixed = batch.model_copy(update={"rows": (batch.rows[0], bad_row, batch.rows[1])})
    result = await container.knowledge.import_export(mixed, OWNER)
    assert [r.status for r in result.rows] == ["accepted", "rejected", "accepted"]
    assert result.rows[1].error_code == "invalid_input"
    assert "PRIVATE_EXTRA_KEY_CANARY" not in result.model_dump_json()
    assert await container.knowledge.import_export(mixed, OWNER) == result
    repaired = await container.knowledge.import_export(batch, OWNER)
    assert [r.source_id for r in repaired.rows] == [
        result.rows[0].source_id,
        result.rows[2].source_id,
    ]
    records = await container.knowledge.list(OWNER)
    assert len(records) == 2 and all(
        r.revision_number == 1 and r.document.synthetic for r in records
    )
    assert all(r.provenance.provider == batch.provider for r in records)
    assert {r.document.content for r in records} == {row["body"] for row in batch.rows}


async def test_import_opaque_revision_replay_does_not_rewind_head(container):
    batch = KnowledgeExport(
        provider="confluence",
        namespace="DEMO",
        domain_id="triv3",
        rows=({"id": "42", "version": "z-before", "title": "plan", "body": "first"},),
    )
    first = (await container.knowledge.import_export(batch, OWNER)).rows[0]
    newer = batch.model_copy(
        update={
            "rows": (
                batch.rows[0]
                | {
                    "version": "a-after",
                    "body": "second",
                    "expected_revision": first.source_revision,
                },
            )
        }
    )
    second = (await container.knowledge.import_export(newer, OWNER)).rows[0]
    assert (await container.knowledge.import_export(batch, OWNER)).rows[0] == first
    assert (
        await container.knowledge.get(first.source_id, OWNER)
    ).document.source_revision == second.source_revision
    conflict = batch.model_copy(
        update={"rows": (batch.rows[0] | {"body": "changed same version"},)}
    )
    denied = (await container.knowledge.import_export(conflict, OWNER)).rows[0]
    assert denied.status == "rejected" and denied.error_code == "idempotency_conflict"


async def test_http_crud_uses_resolved_identity_and_sanitized_errors(container):
    app = create_app(container=container)
    local = await container.repository.local_principal()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://test"
    ) as client:
        for field in (
            "owner_id",
            "roles",
            "thread_id",
            "membership",
            "PRIVATE_CANARY_UNKNOWN_FIELD",
        ):
            invalid = await client.post(
                "/v1/knowledge/sources", json=note().model_dump(mode="json") | {field: "forged"}
            )
            assert invalid.status_code == 422 and field not in invalid.text
        response = await client.post("/v1/knowledge/sources", json=note().model_dump(mode="json"))
        assert response.status_code == 201
        record = response.json()
        source_id = record["document"]["source_id"]
        assert record["document"]["owner_id"] == local.user_id
        assert record["document"]["content"] == note().content
        assert len((await client.get("/v1/knowledge/sources")).json()) == 1
        update = note(
            provider_revision="next",
            expected_revision=record["document"]["source_revision"],
            content="new",
        )
        second = await client.put(
            f"/v1/knowledge/sources/{source_id}", json=update.model_dump(mode="json")
        )
        assert second.status_code == 200
        assert (
            await client.put(
                f"/v1/knowledge/sources/{source_id}", json=update.model_dump(mode="json")
            )
        ).json() == second.json()

        async def other_identity():
            return OTHER  # Authenticated test transport identity, never a body/header grant.

        app.dependency_overrides[resolve_principal] = other_identity
        denied = await client.get(f"/v1/knowledge/sources/{source_id}")
        missing = await client.get("/v1/knowledge/sources/absent")
        assert denied.status_code == missing.status_code == 404 and denied.json() == missing.json()
        app.dependency_overrides.clear()
        deleted = await client.request(
            "DELETE",
            f"/v1/knowledge/sources/{source_id}",
            json={
                "expected_revision": second.json()["document"]["source_revision"],
                "mutation_id": "api-delete",
            },
        )
        assert deleted.status_code == 200 and deleted.json()["deleted"]
        assert (await client.get(f"/v1/knowledge/sources/{source_id}")).status_code == 404


async def test_restart_cas_history_and_seed_do_not_restore_deleted_source(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'store.db'}",
        trace_dir=tmp_path / "trace",
    )
    first = build_container(settings)
    await first.startup()
    owner = await first.repository.local_principal()
    original = await first.knowledge.write(note(), owner)
    source_id = original.document.source_id
    deleted = await first.knowledge.delete(
        source_id,
        KnowledgeDelete(expected_revision=original.document.source_revision, mutation_id="gone"),
        owner,
    )
    await first.shutdown()
    second = build_container(settings)
    await second.startup()
    try:
        assert await second.repository.local_principal() == owner
        assert await second.knowledge.list(owner) == []
        assert (
            await second.knowledge.get(source_id, owner, revision=original.document.source_revision)
            == original
        )
        assert (
            await second.knowledge.get(source_id, owner, revision=deleted.document.source_revision)
            == deleted
        )
        assert await second.knowledge.write(note(), owner) == original
        assert await second.knowledge.list(owner) == []
        with sqlite3.connect(second.repository.path) as db:
            versions = [row[0] for row in db.execute(
                "SELECT version FROM rfa_schema_migrations ORDER BY rowid"
            )]
            # Exactly consecutive 1..N, applied in order; N >= 13 (P0-025 event feed).
            assert versions == list(range(1, len(versions) + 1)) and len(versions) >= 13
            assert db.execute("SELECT count(*) FROM installation_seeds").fetchone()[0] == 1
    finally:
        await second.shutdown()


def legacy(source, revision="v1", **changes):
    return KnowledgeDocument.model_validate(
        dict(
            source_id=source,
            source_revision=revision,
            domain_id="triv3",
            title="legacy",
            content="legacy text",
            location={"uri": "fixture://legacy"},
            audience="public",
            classification="public",
            policy_version="local-v1",
        )
        | changes
    )


async def test_legacy_migration_preserves_rows_without_guessing_head_or_owner(tmp_path):
    path = tmp_path / "legacy.db"
    docs = [
        legacy("single-public"),
        legacy("unknown-private", audience="private"),
        legacy("ambiguous", "z"),
        legacy("ambiguous", "a"),
    ]
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE kb_documents(source_id TEXT, source_revision TEXT, "
            "domain_id TEXT, document_json TEXT, updated_at TEXT, "
            "PRIMARY KEY(source_id,source_revision))"
        )
        for doc in docs:
            db.execute(
                "INSERT INTO kb_documents VALUES (?,?,?,?,?)",
                (
                    doc.source_id,
                    doc.source_revision,
                    "triv3",
                    doc.model_dump_json(),
                    "2026-09-26T00:00:00Z",
                ),
            )
    repo = SqliteWorkRepository(path)
    await repo.initialize()
    await repo.initialize()
    assert await repo.list_documents("triv3") == [docs[0]]
    with sqlite3.connect(path) as db:
        assert {row[0] for row in db.execute("SELECT document_json FROM kb_documents")} == {
            d.model_dump_json() for d in docs
        }
        assert db.execute(
            "SELECT current_revision FROM kb_sources WHERE source_id='ambiguous'"
        ).fetchone() == (None,)
    for source in ("single-public", "unknown-private", "ambiguous"):
        with pytest.raises(RfaError) as error:
            await repo.get_knowledge(source, OWNER)
        assert error.value.code == "not_found"
    await repo.seed_documents_once([legacy("must-not-seed")])
    assert await repo.list_documents("triv3") == [docs[0]]


async def test_legacy_upsert_no_overwrite_or_managed_write_bypass(container):
    doc = legacy("legacy-insert")
    await container.repository.upsert_documents([doc])
    await container.repository.upsert_documents([doc])
    with pytest.raises(RfaError) as error:
        await container.repository.upsert_documents(
            [doc.model_copy(update={"content": "overwrite"})]
        )
    assert error.value.code == "idempotency_conflict"
    await container.repository.upsert_documents(
        [doc.model_copy(update={"source_revision": "another"})]
    )
    assert not [
        d
        for d in await container.repository.list_documents("triv3")
        if d.source_id == doc.source_id
    ]
    created = await container.knowledge.write(note(), OWNER)
    bypass = KnowledgeDocument.model_validate(
        created.document.model_dump(exclude={"project_id"})
        | {"schema_version": "1.0", "source_revision": "bypass"}
    )
    with pytest.raises(RfaError):
        await container.repository.upsert_documents([bypass])
    assert await container.knowledge.get(created.document.source_id, OWNER) == created


async def test_application_db_and_live_sidecars_are_private(tmp_path):
    repo = SqliteWorkRepository(tmp_path / "private.db")
    old = os.umask(0o022)
    try:
        await repo.initialize()
        with repo._connect() as connection:
            connection.execute("SELECT * FROM kb_sources").fetchall()
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(repo.path) + suffix)
                info = os.stat(path)
                assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    finally:
        os.umask(old)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_privacy_guard_handles_only_sidecar_unlink_between_open_and_stat(
    tmp_path, monkeypatch, suffix
):
    repo = SqliteWorkRepository(tmp_path / "race.db")
    repo.path.touch(mode=0o600)
    target = Path(str(repo.path) + suffix)
    target.touch(mode=0o600)
    original_open = os.open

    def unlink_after_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == target:
            os.unlink(target)  # Deterministic last-SQLite-connection sidecar cleanup race.
        return descriptor

    monkeypatch.setattr(os, "open", unlink_after_open)
    if not suffix:
        with pytest.raises(RfaError) as error:
            repo._private_files()
        assert error.value.code == "configuration_error"
    else:
        repo._private_files()
        assert not target.exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_privacy_guard_still_rejects_multiple_hardlinks(tmp_path, suffix):
    repo = SqliteWorkRepository(tmp_path / "hardlink.db")
    repo.path.touch(mode=0o600)
    target = Path(str(repo.path) + suffix)
    target.touch(mode=0o600)
    os.link(target, tmp_path / "other-link")
    with pytest.raises(RfaError) as error:
        repo._private_files()
    assert error.value.code == "configuration_error"


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
@pytest.mark.parametrize("kind", ["symlink", "dangling", "fifo"])
async def test_application_db_nonregular_targets_fail_closed(tmp_path, suffix, kind):
    repo = SqliteWorkRepository(tmp_path / "unsafe.db")
    target = Path(str(repo.path) + suffix)
    other = tmp_path / "unrelated"
    if kind == "fifo":
        os.mkfifo(target)
    elif kind == "dangling":
        os.symlink(other, target)
    else:
        other.touch(mode=0o640)
        os.symlink(other, target)
    with pytest.raises(RfaError) as error:
        await repo.initialize()
    assert error.value.code == "configuration_error"
    if kind == "symlink":
        assert stat.S_IMODE(other.stat().st_mode) == 0o640
    if kind == "dangling":
        assert not other.exists()


def test_knowledge_input_rejects_authority_fields_and_bad_acl():
    for injected in ({"owner_id": "forged"}, {"roles": ["admin"]}):
        with pytest.raises(ValidationError):
            note(**injected)
    with pytest.raises(ValidationError):
        note(acl={"audience": "business_unit", "company_id": "company-a"})


@pytest.mark.parametrize(
    "receipt",
    [
        {"status": "accepted"},
        {
            "status": "accepted",
            "source_id": "source",
            "source_revision": "revision",
            "error_code": "not_found",
        },
        {"status": "rejected"},
        {"status": "rejected", "source_id": "source", "error_code": "not_found"},
    ],
)
def test_partial_receipt_cannot_claim_impossible_success(receipt):
    with pytest.raises(ValidationError):
        KnowledgeImportRow(row=0, **receipt)


async def test_invalid_import_rows_do_not_bypass_authentication(container):
    batch = KnowledgeExport(provider="confluence", namespace="DEMO", domain_id="triv3", rows=({},))
    with pytest.raises(RfaError) as error:
        await container.knowledge.import_export(
            batch, OWNER.model_copy(update={"authenticated": False})
        )
    assert error.value.code == "authentication_required"


# -- P1-004A reviewed knowledge accumulation --------------------------------------------
from rfa_mas.contracts import DirectWorkRequest as _Direct  # noqa: E402
from rfa_mas.contracts import DomainId as _Domain  # noqa: E402
from rfa_mas.contracts import DraftTarget as _Target  # noqa: E402
from rfa_mas.contracts import TeamExecutionRequest as _TeamRequest  # noqa: E402
from rfa_mas.bootstrap import build_container as _build  # noqa: E402
from rfa_mas.settings import Settings as _Settings  # noqa: E402


async def _kb_container(tmp_path):
    container = _build(_Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'acc.db'}",
                                 trace_dir=(tmp_path / "traces").resolve()))
    await container.startup()
    return container, await container.repository.local_principal()


def _note(key, content, audience="owner", revision="r1", expected=None):
    return KnowledgeWrite.model_validate({
        "domain_id": "triv3", "provenance": {"provider": "note", "namespace": "acc",
                                             "external_id": key},
        "provider_revision": revision, "expected_revision": expected, "title": f"note {key}",
        "content": content, "synthetic": True, "acl": {"audience": audience}})


async def test_supervisor_gate_labels_extracted_items_and_never_promotes_inference(tmp_path):
    container, owner = await _kb_container(tmp_path)
    acc = container.knowledge.accumulator
    try:
        await container.knowledge.write(_note("plan", "\n".join([
            "결정: SDK 공개 출시일은 2026-10-20이다.",
            "B 결과 환경 checksum 확인 필요, 10월 2일 마감",
            "다른 방법의 6.0ms는 검증 전 가설이다.",
            "참고 문서 https://docs.example.invalid/sdk",
        ])), owner)
        proposals = await acc.extract(_Domain.TRIV3, owner)
        kinds = sorted((p.kind, p.epistemic_state) for p in proposals)
        assert ("decision", "cited") in kinds and ("todo", "cited") in kinds
        assert ("summary", "tentative") in kinds and ("link", "cited") in kinds
        # Proposals are not knowledge yet: nothing derived before the gate.
        assert await acc.list_derived(_Domain.TRIV3, owner) == []
        report = await acc.accumulate(_Domain.TRIV3, proposals, owner)
        assert all(i.review_state == "accepted" for i in report.items)
        derived = await acc.list_derived(_Domain.TRIV3, owner)
        assert {d.epistemic_state for d in derived} == {"cited", "tentative"}
        assert all(len(d.parents) == 1 for d in derived)
        forged = proposals[0].model_copy(update={"content": "출시일은 이미 확정 완료되었다",
                                                 "title": "uncited", "origin_ref": "forged"})
        conflict = [proposals[0].model_copy(update={"title": "출시 결정", "kind": "decision",
                                                    "origin_ref": f"c{i}", "content": text})
                    for i, text in enumerate(("결정: SDK 공개 출시일은 2026-10-20이다.",
                                              "출시일은 2026-10-27로 결정"))]
        second = await acc.accumulate(_Domain.TRIV3, [forged, *conflict], owner)
        states = {i.title: (i.epistemic_state, i.reason) for i in second.items}
        assert states["uncited"] == ("inferred", "downgraded_uncited")
        assert states["출시 결정"][0] == "conflicting"
    finally:
        await container.shutdown()


async def test_stale_or_restricted_parent_excludes_derived_items(tmp_path):
    container, owner = await _kb_container(tmp_path)
    acc = container.knowledge.accumulator
    try:
        public = await container.knowledge.write(
            _note("faq", "결정: 공개 FAQ 설치 절차는 v1이다.", audience="public"), owner)
        private = await container.knowledge.write(
            _note("mine", "결정: 개인 자원은 공유하지 않는다.", audience="owner"), owner)
        report = await acc.accumulate(_Domain.TRIV3, await acc.extract(_Domain.TRIV3, owner), owner)
        assert len([i for i in report.items if i.review_state == "accepted"]) == 2
        public_view = await container.repository.authorized_metadata(
            _Domain.TRIV3, owner, target=Audience.PUBLIC)
        derived_public = [m for m in public_view if m.parents]
        assert len(derived_public) == 1  # Only the item whose every parent is public.
        assert all(p.source_id == public.document.source_id for p in derived_public[0].parents)
        stale_proposals = await acc.extract(_Domain.TRIV3, owner)
        await container.knowledge.write(_note("faq", "결정: 공개 FAQ 설치 절차는 v2이다.",
                                              audience="public", revision="r2",
                                              expected=public.document.source_revision), owner)
        # The old derived item no longer survives the current-parent closure.
        derived = await acc.list_derived(_Domain.TRIV3, owner)
        assert all(p.source_id != public.document.source_id for d in derived for p in d.parents)
        replay = await acc.accumulate(_Domain.TRIV3, stale_proposals, owner)
        rejected = [i for i in replay.items if i.reason == "stale_or_restricted_parent"]
        assert rejected and all(i.review_state == "rejected" for i in rejected)
        assert private.document.source_id
    finally:
        await container.shutdown()


async def test_completed_team_run_accumulates_simulated_and_tentative_items(tmp_path):
    container, owner = await _kb_container(tmp_path)
    try:
        for key, content in (("a", "합성 benchmark A 로그. 환경 env-1, 지연 10.0ms, 정확도 81.0%"),
                             ("b", "합성 benchmark B 로그. 환경 env-1, 지연 8.2ms, 정확도 80.8%"),
                             ("m", "다른 방법 benchmark 지연 6.0ms는 검증 전 가설")):
            await container.knowledge.write(_note(key, content), owner)
        result = await container.service.run(_Direct(
            query="TRIV3 benchmark 로그 검증", domain_id=_Domain.TRIV3,
            target=_Target(audience=Audience.OWNER),
            team=_TeamRequest(goal="TRIV3 benchmark 로그 검증과 지연 비교",
                              outputs=("benchmark_report",))), owner)
        assert result.status.value == "completed"
        derived = await container.knowledge.accumulator.list_derived(_Domain.TRIV3, owner)
        states = sorted(d.epistemic_state for d in derived)
        assert states == ["simulated", "tentative"]
        simulated = next(d for d in derived if d.epistemic_state == "simulated")
        assert len(simulated.parents) == 2
        body = (await container.repository.read_sources(
            _Domain.TRIV3, owner, [simulated.reference]))[0].content
        assert "-18.0%" in body and "-0.2%p" in body and "실측이 아님" in body
    finally:
        await container.shutdown()
