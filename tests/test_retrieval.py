"""Actual SQLite/local policy tests; no network, real secrets, or publication authority."""
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rfa_mas.adapters.local import LocalPolicy, SqliteWorkRepository
from rfa_mas.adapters.retrieval import BoundContextReader, LocalRetrieval
from rfa_mas.api.app import create_app, resolve_principal
from rfa_mas.application.knowledge import KnowledgeService
from rfa_mas.application.source_access import BoundAccess
from rfa_mas.contracts import (
    Audience, ContextLevel, ContextRequest, DirectWorkRequest, DomainId, DraftTarget,
    EvidenceItem, KnowledgeDelete, KnowledgeExport, KnowledgeWrite, PolicyBindings,
    ResumeRequest, RetrievalRequest, SourceRevisionRef, TrustedPrincipal, sha256_text,
)
from rfa_mas.errors import RfaError

OWNER = TrustedPrincipal(user_id="reader-owner",authenticated=True,company_id="company-a",
                         business_units=frozenset({"unit-a"}))
OTHER = TrustedPrincipal(user_id="reader-other",authenticated=True,company_id="company-b",
                         business_units=frozenset({"unit-a"}))
RAW = " \t한글 근거\r\n  launch remains tentative \t\n"


def note(identity="one", **changes):
    return KnowledgeWrite.model_validate({
        "domain_id":"triv3", "provenance":{"provider":"note","namespace":"retrieval",
            "external_id":identity}, "provider_revision":"opaque-z", "title":"needle title",
        "content":"needle fact", "synthetic":True, **changes,
    })


@pytest.fixture
async def kb(tmp_path):
    grants = {("reader-owner","company-a"):frozenset({"project-a"})}
    repo = SqliteWorkRepository(tmp_path / "kb.db",
        project_resolver=lambda user,company: grants.get((user,company),frozenset()))
    await repo.initialize()
    return repo, KnowledgeService(repo,LocalPolicy()), grants


async def reference(repo, record, principal=OWNER):
    metadata = await repo.authorized_metadata(DomainId.TRIV3,principal)
    return next(m.reference for m in metadata if m.reference.source_id==record.document.source_id)


def request(principal=OWNER, **changes):
    return ContextRequest.model_validate({
        "request_id":"req-context", "trace_id":"trace-context", "run_id":"run-context",
        "agent_id":"source-scout", "domain_id":"triv3", "query":"needle",
        "allowed_audiences":list(Audience), "principal":principal,
        "goal":"Find current cited facts", "role":"source_scout",
        "target":{"audience":"owner"}, "endpoint_id":"local-model", **changes,
    })


def reader(repo, principal=lambda: OWNER, **options):
    bound = BoundAccess(principal,"source-scout","source_scout",DomainId.TRIV3,
                        DraftTarget(audience=Audience.OWNER),"local-model")
    return BoundContextReader(repo,LocalPolicy(),bound,issuer_supported=True,**options)


def body_spy(repo, monkeypatch):
    reads=[]
    original=repo._read_source_body
    def spy(connection, metadata):
        reads.append(metadata.reference.source_id)
        return original(connection,metadata)
    monkeypatch.setattr(repo,"_read_source_body",spy)
    return reads


async def test_metadata_first_l0_selected_l2_raw_exact_and_frozen_legacy(kb,monkeypatch):
    repo,service,_=kb
    first=await service.write(note(content=RAW),OWNER)
    await service.write(note("second",content="needle unrelated second"),OWNER)
    ref=await reference(repo,first)
    reads=body_spy(repo,monkeypatch)
    adapter=reader(repo)
    req=request(selected_sources=(ref,))
    receipts=await adapter.issue(req)
    assert reads==[]
    l0=await adapter.load_context(req.model_copy(update={"policies":receipts}))
    assert l0.items[0].excerpt=="" and l0.loaded_characters==0 and reads==[]
    l2=await adapter.load_context(req.model_copy(update={"level":ContextLevel.L2,"policies":receipts}))
    assert l2.items[0].excerpt==RAW and l2.loaded_characters==len(RAW)
    assert json.loads(l2.model_dump_json())["items"][0]["excerpt"]==RAW
    assert l2.items[0].content_hash==l2.items[0].parents[0].content_hash==sha256_text(RAW)
    assert l2.measured_tokens is None and reads==[first.document.source_id]
    legacy=EvidenceItem(source_id=ref.source_id,source_revision=ref.source_revision,
        location=ref.location,audience=ref.audience,excerpt=RAW,content_hash=sha256_text(RAW),
        policy_version="local-v1")
    assert legacy.excerpt==RAW.strip()  # The frozen1.0 boundary has NOT changed.
    decision=adapter.decision(receipts.read_decision_id)
    assert decision.allowed and decision.source_refs==(ref,) and not decision.simulated


async def test_search_filters_before_body_and_no_match_has_no_fallback(kb,monkeypatch):
    repo,service,_=kb
    public=await service.write(note("public",acl={"audience":"public"}),OWNER)
    private=await service.write(note("private",content="needle PRIVATE_CANARY"),OWNER)
    company=await service.write(note("company",acl={"audience":"company","company_id":"company-a"}),OWNER)
    unit=await service.write(note("unit",acl={"audience":"business_unit","company_id":"company-a",
                                         "memberships":["unit-a"]}),OWNER)
    reads=body_spy(repo,monkeypatch)
    adapter=LocalRetrieval(repo,policy_version="local-v1")
    req=RetrievalRequest(request_id="r",trace_id="t",run_id="u",agent_id="a",
        domain_id=DomainId.TRIV3,query="needle",allowed_audiences=tuple(Audience),principal=OTHER)
    result=await adapter.search(req)
    assert {i.source_id for i in result.items}=={public.document.source_id}
    assert not result.simulated and reads==[public.document.source_id]
    assert "PRIVATE_CANARY" not in result.model_dump_json()
    own=await adapter.search(req.model_copy(update={"principal":OWNER}))
    assert {i.source_id for i in own.items}=={r.document.source_id for r in (public,private,company,unit)}
    # P1-001D: BM25 score first (the longer private body scores lower), then the stable
    # provider/namespace/external_id key; never the random server-generated source_id.
    assert [i.source_id for i in own.items]==[
        r.document.source_id for r in (company,public,unit,private)]
    reads.clear()
    missing=await adapter.search(req.model_copy(update={"query":"NO_MATCH_XQZ"}))
    assert missing.insufficient and not missing.items and reads==[]


async def test_owner_read_is_not_public_share_and_metadata_never_leaks(kb,monkeypatch):
    repo,service,_=kb
    private=await service.write(note(title="PRIVATE_TITLE_CANARY",content="needle secret"),OWNER)
    ref=await reference(repo,private)
    reads=body_spy(repo,monkeypatch)
    assert await repo.authorized_metadata(DomainId.TRIV3,OWNER,target=Audience.PUBLIC)==[]
    bound=BoundAccess(lambda:OWNER,"source-scout","source_scout",DomainId.TRIV3,
                     DraftTarget(audience=Audience.PUBLIC),"local-model")
    adapter=BoundContextReader(repo,LocalPolicy(),bound,issuer_supported=True)
    with pytest.raises(RfaError):
        await adapter.issue(request(selected_sources=(ref,),target=bound.target))
    assert reads==[]


async def test_project_default_deny_and_company_membership_is_not_project_authority(kb,tmp_path):
    repo,service,grants=kb
    scoped=note(acl={"audience":"company","company_id":"company-a","project_id":"project-a"})
    record=await service.write(scoped,OWNER)
    no_grants=SqliteWorkRepository(repo.path)
    assert await no_grants.authorized_metadata(DomainId.TRIV3,OWNER)==[]
    with pytest.raises(RfaError):
        await no_grants.write_knowledge(scoped,OWNER,policy_version="local-v1")
    same_unit_wrong_company=OTHER.model_copy(update={"roles":frozenset({"project-a","company"})})
    assert await repo.authorized_metadata(DomainId.TRIV3,same_unit_wrong_company)==[]
    assert record.document.project_id=="project-a"


async def test_current_project_restriction_applies_before_old_replay_import_and_history(kb,monkeypatch):
    repo,service,grants=kb
    old_request=note(acl={"audience":"public"})
    old=await service.write(old_request,OWNER)
    newer=await service.write(note(provider_revision="opaque-a",
        expected_revision=old.document.source_revision,
        acl={"audience":"company","company_id":"company-a","project_id":"project-a"}),OWNER)
    grants.clear()
    reads=[]
    original=repo._knowledge_revision
    def spy(*args): reads.append(True); return original(*args)
    monkeypatch.setattr(repo,"_knowledge_revision",spy)
    for action in (service.write(old_request,OWNER),
                   service.get(old.document.source_id,OWNER,revision=old.document.source_revision),
                   service.get(old.document.source_id,OWNER)):
        with pytest.raises(RfaError): await action
    exported=KnowledgeExport(provider="github_issue",namespace="export",domain_id=DomainId.TRIV3,rows=(
        {"number":"x","revision":"r","title":"t","body":"c"},))
    # Replay of an actual export follows the same repository current-head gate.
    grants[(OWNER.user_id,OWNER.company_id)]=frozenset({"project-a"})
    imported=await service.import_export(exported,OWNER)
    imported_id=imported.rows[0].source_id
    imported_record=await service.get(imported_id,OWNER)
    original_request=KnowledgeWrite(domain_id=exported.domain_id,
        provenance={"provider":"github_issue","namespace":"export","external_id":"x"},
        provider_revision="r2",expected_revision=imported_record.document.source_revision,
        title="t",content="c",acl={"audience":"company","company_id":"company-a","project_id":"project-a"})
    await service.write(original_request,OWNER)
    grants.clear(); reads.clear()
    replay=await service.import_export(exported,OWNER)
    assert replay.rows[0].status=="rejected" and reads==[]
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT current_revision FROM kb_sources WHERE source_id=?",
                          (old.document.source_id,)).fetchone()[0]==newer.document.source_revision


@pytest.mark.parametrize("mutation",["revision","delete","policy","acl"])
async def test_all_parent_changes_invalidate_derived_metadata_body_and_owner_history(kb,monkeypatch,mutation):
    repo,service,_=kb
    a=await service.write(note("A",acl={"audience":"public"}),OWNER)
    b=await service.write(note("B",acl={"audience":"public"}),OWNER)
    parents=(await reference(repo,a),await reference(repo,b))
    derived=await repo.write_derived_knowledge(note("summary",acl={"audience":"public"}),OWNER,
        parents=parents,policy_version="local-v1",epistemic_state="tentative")
    dref=await reference(repo,derived)
    adapter=reader(repo)
    req=request(selected_sources=(dref,),level="L1")
    receipts=await adapter.issue(req)
    result=await adapter.load_context(req.model_copy(update={"policies":receipts}))
    assert result.items[0].epistemic_state=="tentative" and result.items[0].parents==parents
    if mutation=="delete":
        await service.delete(b.document.source_id,KnowledgeDelete(
            expected_revision=b.document.source_revision,mutation_id="delete-b"),OWNER)
    else:
        changed=note("B",provider_revision="second",expected_revision=b.document.source_revision,
            acl={"audience":"private"} if mutation=="acl" else {"audience":"public"})
        await repo.write_knowledge(changed,OWNER,policy_version="local-v2" if mutation=="policy" else "local-v1")
    reads=body_spy(repo,monkeypatch)
    with pytest.raises(RfaError): await adapter.load_context(req.model_copy(update={"policies":receipts}))
    with pytest.raises(RfaError): await service.get(derived.document.source_id,OWNER)
    assert derived.document.source_id not in {m.reference.source_id for m in
        await repo.authorized_metadata(DomainId.TRIV3,OWNER)}
    assert reads==[]


async def test_parent_fingerprint_cannot_drop_private_ancestor(kb):
    repo,service,_=kb
    a=await service.write(note("A",acl={"audience":"public"}),OWNER)
    b=await service.write(note("B"),OWNER)
    parents=(await reference(repo,a),await reference(repo,b))
    write=note("summary",acl={"audience":"public"})
    result=await repo.write_derived_knowledge(write,OWNER,parents=parents,policy_version="local-v1")
    with pytest.raises(RfaError) as error:
        await repo.write_derived_knowledge(write,OWNER,parents=parents[:1],policy_version="local-v1")
    assert error.value.code=="idempotency_conflict"
    assert result.document.source_id not in {m.reference.source_id for m in
        await repo.authorized_metadata(DomainId.TRIV3,OWNER,target=Audience.PUBLIC)}
    assert await repo.write_derived_knowledge(write,OWNER,parents=parents,policy_version="local-v1")==result


@pytest.mark.parametrize("reverse",[False,True])
async def test_diamond_checks_each_revision_order_independent(kb,reverse):
    repo,service,_=kb
    a1=await service.write(note("A"),OWNER)
    ar1=await reference(repo,a1)
    b1=await repo.write_derived_knowledge(note("B1"),OWNER,parents=(ar1,),policy_version="local-v1")
    br1=await reference(repo,b1)
    a2=await service.write(note("A",provider_revision="new",expected_revision=a1.document.source_revision),OWNER)
    ar2=await reference(repo,a2)
    b2=await repo.write_derived_knowledge(note("B2"),OWNER,parents=(ar2,),policy_version="local-v1")
    br2=await reference(repo,b2)
    assert ar1.content_hash==ar2.content_hash and ar1.source_revision!=ar2.source_revision
    parents=(br2,br1) if reverse else (br1,br2)
    with pytest.raises(RfaError):
        await repo.write_derived_knowledge(note("C"),OWNER,parents=parents,policy_version="local-v1")
    # A test-only corrupted old diamond still cannot bypass reader validation.
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE kb_revision_context SET parent_refs=? WHERE source_id=?",
            (json.dumps([p.model_dump(mode="json") for p in parents]),b2.document.source_id))
    assert b2.document.source_id not in {m.reference.source_id for m in
        await repo.authorized_metadata(DomainId.TRIV3,OWNER)}


@pytest.mark.parametrize("damage",["missing","cycle","missing_parent"])
async def test_corrupt_or_missing_lineage_fails_closed(kb,damage,monkeypatch):
    repo,service,_=kb
    a=await service.write(note("A"),OWNER); ar=await reference(repo,a)
    child=await repo.write_derived_knowledge(note("B"),OWNER,parents=(ar,),policy_version="local-v1")
    br=await reference(repo,child)
    with sqlite3.connect(repo.path) as db:
        refs=[] if damage=="missing" else [br.model_dump(mode="json") if damage=="cycle"
            else ar.model_copy(update={"source_id":"missing-source"}).model_dump(mode="json")]
        db.execute("UPDATE kb_revision_context SET parent_refs=? WHERE source_id=?",
            (json.dumps(refs),child.document.source_id))
    reads=body_spy(repo,monkeypatch)
    with pytest.raises(RfaError): await repo.read_sources(DomainId.TRIV3,OWNER,(br,))
    assert reads==[]


async def test_project_grant_revocation_invalidates_issued_receipt_before_body(kb,monkeypatch):
    repo,service,grants=kb
    source=await service.write(note(acl={"audience":"company","company_id":"company-a","project_id":"project-a"}),OWNER)
    ref=await reference(repo,source)
    adapter=reader(repo); req=request(selected_sources=(ref,),level="L2")
    receipts=await adapter.issue(req)
    grants.clear()
    reads=body_spy(repo,monkeypatch)
    with pytest.raises(RfaError): await adapter.load_context(req.model_copy(update={"policies":receipts}))
    assert reads==[]


@pytest.mark.parametrize("damage",["fabricated","expired","restart","principal","agent","role",
                                  "target","channel","endpoint","policy","source"])
async def test_receipt_bindings_are_live_and_fail_before_body(kb,monkeypatch,damage):
    repo,service,_=kb
    a=await service.write(note(),OWNER); ref=await reference(repo,a)
    now=[datetime(2026,9,26,tzinfo=UTC)]
    actor=[OWNER]
    adapter=reader(repo,principal=lambda:actor[0],clock=lambda:now[0])
    req=request(selected_sources=(ref,),level="L2")
    receipts=await adapter.issue(req)
    req=req.model_copy(update={"policies":receipts})
    if damage=="fabricated":
        req=req.model_copy(update={"policies":PolicyBindings(read_decision_id="forged",
            share_decision_id="forged",egress_decision_id="forged")})
    elif damage=="expired": now[0]+=timedelta(seconds=61)
    elif damage=="restart": adapter=reader(repo)
    elif damage=="principal": actor[0]=OTHER
    elif damage in {"agent","role","endpoint"}:
        req=req.model_copy(update={{"agent":"agent_id","role":"role","endpoint":"endpoint_id"}[damage]:"forged"})
    elif damage in {"target","channel"}:
        target=req.target.model_copy(update={"audience":Audience.PUBLIC} if damage=="target" else {"channel":"external"})
        req=req.model_copy(update={"target":target})
    elif damage=="policy": adapter.policy.policy_version="local-v2"
    elif damage=="source":
        await service.write(note(provider_revision="next",expected_revision=ref.source_revision),OWNER)
    reads=body_spy(repo,monkeypatch)
    with pytest.raises(RfaError): await adapter.load_context(req)
    assert reads==[]


async def test_unknown_endpoint_denied_before_policy_and_empty_l1_budget_not_fabricated(kb,monkeypatch):
    repo,service,_=kb
    a=await service.write(note(content=RAW),OWNER); ref=await reference(repo,a)
    adapter=reader(repo)
    req=request(selected_sources=(ref,))
    calls=[]
    original=adapter.policy.evaluate
    async def spy(*args,**kwargs): calls.append(True); return await original(*args,**kwargs)
    monkeypatch.setattr(adapter.policy,"evaluate",spy)
    with pytest.raises(RfaError): await adapter.issue(req.model_copy(update={"endpoint_id":"cloud-model"}))
    assert calls==[]
    receipts=await adapter.issue(req)
    assert len(calls)==3
    l1=await adapter.load_context(req.model_copy(update={"policies":receipts,"level":ContextLevel.L1}))
    assert l1.insufficient and l1.loaded_characters==0 and l1.items==()
    tiny=await adapter.load_context(req.model_copy(update={"policies":receipts,"level":ContextLevel.L2,"max_characters":1}))
    assert tiny.insufficient and tiny.loaded_characters==0 and tiny.measured_tokens is None
    with pytest.raises(RfaError): await adapter.issue(request(query="NO_MATCH_ABC"))


async def test_old_none_project_fingerprint_survives_restart(kb):
    repo,service,_=kb
    from rfa_mas.adapters.local import _canonical_fingerprint
    original=note()
    record=await service.write(original,OWNER)
    legacy=original.model_dump(mode="json",exclude={"expected_revision"})
    legacy["acl"].pop("project_id")
    with sqlite3.connect(repo.path) as db:
        stored=db.execute("SELECT fingerprint FROM kb_source_revisions WHERE source_id=?",
                          (record.document.source_id,)).fetchone()[0]
        assert stored==_canonical_fingerprint(legacy)
    await repo.initialize()
    assert await service.write(original,OWNER)==record


async def test_current_outward_result_and_session_redact_without_mutating_draft(container,principal,monkeypatch):
    write=note(content="needle HISTORY_CANARY",acl={"audience":"public"})
    record=await container.knowledge.write(write,principal)
    result=await container.service.run(DirectWorkRequest(query="needle",domain_id=DomainId.TRIV3,
        target=DraftTarget(audience=Audience.PUBLIC)),principal)
    assert result.draft and "HISTORY_CANARY" in result.draft.content
    run=await container.repository.get_owned_run(result.run_id,principal)
    def stored_draft():
        with sqlite3.connect(container.repository.path) as db:
            return db.execute("SELECT content_hash,draft_json FROM drafts WHERE draft_id=?",
                              (result.draft.draft_id,)).fetchall()
    before=stored_draft()
    assert before
    await container.knowledge.write(note(provider_revision="next",expected_revision=record.document.source_revision,
        content=write.content,acl={"audience":"private"}),principal)
    async def forbidden(*args,**kwargs): raise AssertionError("GET/terminal resume must not execute ports")
    monkeypatch.setattr(container.runtime,"run",forbidden)
    monkeypatch.setattr(container.response,"submit_draft",forbidden)
    monkeypatch.setattr(container.response,"get_decision",forbidden)
    projected=await container.service.get(result.run_id,principal)
    assert projected.draft is projected.review is None and projected.errors
    resumed=await container.service.resume(result.run_id,ResumeRequest(event_id="terminal"),principal)
    assert resumed.draft is None
    detail=await container.service.present_session(run.session_id,principal)
    assert all("HISTORY_CANARY" not in m.content for m in detail.messages if m.role=="assistant")
    assert [m.content for m in detail.messages if m.role=="user"]==["needle"]
    app=create_app(container=container)
    app.dependency_overrides[resolve_principal]=lambda:principal
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        for path in (f"/v1/work/{result.run_id}",f"/v1/runs/{result.run_id}",f"/v1/sessions/{run.session_id}"):
            response=await client.get(path)
            assert response.status_code==200 and "HISTORY_CANARY" not in response.text
    assert stored_draft()==before
    stored=await container.repository.get_owned_run(result.run_id,principal)
    assert stored.result.draft==result.draft  # Outward projection never re-versions approval content.


# -- P1-001D deterministic Korean-aware lexical ranking ----------------------------------
from rfa_mas.adapters.retrieval import lexical_terms  # noqa: E402


def search_request(principal=OWNER, query="needle", **changes):
    return RetrievalRequest(request_id="r",trace_id="t",run_id="u",agent_id="a",
        domain_id=DomainId.TRIV3,query=query,allowed_audiences=tuple(Audience),
        principal=principal,**changes)


def test_query_terms_drop_particles_and_request_words_and_bound_short_tokens():
    query = "B가 A보다 지연이 얼마나 줄었어? issue #17의 담당자와 알려줘"
    terms = lexical_terms(query)
    assert terms == tuple(sorted(terms)) == lexical_terms(query)
    words = {t for t, _ in terms}
    assert {"지연", "담당자", "담당", "issue"} <= words
    assert not {"지연이", "담당자와", "b가", "a보다", "보다", "얼마나", "알려줘"} & words
    assert {("a", True), ("b", True), ("17", True), ("issue", False)} <= set(terms)


async def test_particles_no_longer_hide_korean_and_latin_matches(kb):
    repo,service,_=kb
    a=await service.write(note("bench-a",title="benchmark A 실행 로그",
                               content="환경: env-01\n지연: 10.0ms\n정확도: 81.0%"),OWNER)
    b=await service.write(note("bench-b",title="benchmark B 실행 로그",
                               content="환경: env-01\n지연: 8.2ms\n정확도: 80.8%"),OWNER)
    await service.write(note("banana",title="바나나 재고",content="banana bread about crabs"),
                        OWNER)
    await service.write(note("other",title="회의록",content="출시 일정과 담당 배정"),OWNER)
    adapter=LocalRetrieval(repo,policy_version="local-v1")
    got=await adapter.search(search_request(query="B가 A보다 지연이 얼마나 줄었어?"))
    # Before P1-001D neither log matched "지연이"/"a보다"/"b가" (insufficient evidence).
    # A short token matches whole tokens only: "banana"/"about" never count as "a"/"b".
    assert {i.source_id for i in got.items}=={a.document.source_id,b.document.source_id}
    exact=await adapter.search(search_request(query="benchmark A의 정확도는 얼마야?"))
    assert exact.items[0].source_id==a.document.source_id


async def test_equal_scores_break_ties_by_provenance_not_random_source_id(tmp_path):
    orders=[]
    for attempt, sequence in enumerate((("c","a","b"),("b","c","a"),("a","b","c"))):
        repo=SqliteWorkRepository(tmp_path / f"tie-{attempt}.db")
        await repo.initialize()
        service=KnowledgeService(repo,LocalPolicy())
        written={}
        for key in sequence:  # Insertion order and server-generated IDs differ per DB.
            record=await service.write(
                note(f"tie-{key}",title="동일 제목",content="같은 지연 기록"),OWNER)
            written[record.document.source_id]=key
        result=await LocalRetrieval(repo,policy_version="local-v1").search(
            search_request(query="지연이 기록된 제목"))
        orders.append([written[i.source_id] for i in result.items])
    assert orders==[["a","b","c"]]*3


async def test_ranking_runs_after_authorization_and_ignores_unreadable_documents(kb):
    repo,service,_=kb
    await service.write(note("public-1",title="공개 지연 안내",content="지연 요약",
                             acl={"audience":"public"}),OWNER)
    await service.write(note("public-2",title="공개 일정",content="일정 안내 지연 없음",
                             acl={"audience":"public"}),OWNER)
    adapter=LocalRetrieval(repo,policy_version="local-v1")
    before=await adapter.search(search_request(OTHER,query="지연이 얼마나 있어?"))
    # Strong matches the other user cannot read must neither appear nor reorder results
    # (no document-frequency side channel from unauthorized documents).
    for index in range(5):
        await service.write(note(f"private-{index}",title="지연 지연 지연",
                                 content="지연 PRIVATE_CANARY 지연"),OWNER)
    after=await adapter.search(search_request(OTHER,query="지연이 얼마나 있어?"))
    assert [i.source_id for i in after.items]==[i.source_id for i in before.items]
    assert len(after.items)==2 and "PRIVATE_CANARY" not in after.model_dump_json()
