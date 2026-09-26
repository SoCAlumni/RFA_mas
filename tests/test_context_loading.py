"""Staged L0/L1/L2 context loader over the real SQLite KB and local policy.

No network, model, secret or external sink. Spies prove which bodies were read, that the
store/policy are not touched for unbound requests, and that the optional ranker (stand-in
for a model/embedding/reranker sink) only runs after authorization on metadata-only items.
"""
import sqlite3

import pytest

from rfa_mas.adapters.local import LocalPolicy, SqliteWorkRepository
from rfa_mas.adapters.retrieval import BoundContextReader
from rfa_mas.application.context import UNTRUSTED_CONTEXT_RULE, ContextLoader, LoadedContext
from rfa_mas.application.knowledge import KnowledgeService
from rfa_mas.application.source_access import BoundAccess
from rfa_mas.contracts import (
    Audience,
    ContextBundle,
    ContextItem,
    ContextLevel,
    ContextRequest,
    DomainId,
    DraftTarget,
    KnowledgeDelete,
    KnowledgeWrite,
    TrustedPrincipal,
)
from rfa_mas.errors import RfaError

OWNER = TrustedPrincipal(user_id="loader-owner", authenticated=True, company_id="company-a",
                         business_units=frozenset({"unit-a"}))
OTHER = TrustedPrincipal(user_id="loader-other", authenticated=True, company_id="company-b")
GOAL = "Summarize the current launch facts"


def note(identity, content, *, title="needle title", audience="private", revision="r1",
         expected=None, acl=None):
    return KnowledgeWrite.model_validate({
        "domain_id": "triv3", "provider_revision": revision, "expected_revision": expected,
        "provenance": {"provider": "note", "namespace": "loader", "external_id": identity},
        "title": title, "content": content, "acl": acl or {"audience": audience},
        "synthetic": True,
    })


@pytest.fixture
async def kb(tmp_path):
    repo = SqliteWorkRepository(tmp_path / "kb.db")
    await repo.initialize()
    return repo, KnowledgeService(repo, LocalPolicy())


async def derive(repo, identity, content, parents, **options):
    return await repo.write_derived_knowledge(
        note(identity, content, **options), OWNER, parents=parents, policy_version="local-v1",
        epistemic_state="tentative")


def sid(record):
    return record.document.source_id


async def ref(repo, record, target=Audience.OWNER):
    metadata = await repo.authorized_metadata(DomainId.TRIV3, OWNER, target=target)
    return next(m.reference for m in metadata if m.reference.source_id == sid(record))


def request(target="owner", **changes):
    return ContextRequest.model_validate({
        "request_id": "req-loader", "trace_id": "trace-loader", "run_id": "run-loader",
        "agent_id": "source-scout", "domain_id": "triv3", "query": "needle",
        "allowed_audiences": list(Audience), "principal": OWNER, "goal": GOAL,
        "role": "source_scout", "target": {"audience": target}, "endpoint_id": "local-model",
        **changes,
    })


def make_loader(repo, target=Audience.OWNER, *, principal=OWNER, endpoint="local-model",
                ranker=None, issuer=True):
    bound = BoundAccess(lambda: principal, "source-scout", "source_scout", DomainId.TRIV3,
                        DraftTarget(audience=target), endpoint)
    reader = BoundContextReader(repo, LocalPolicy(), bound, issuer_supported=issuer)
    return ContextLoader(repo, reader, ranker=ranker)


def spy(monkeypatch, repo, *loaders):
    """Record body reads, store calls and policy evaluations in one ordered event list."""
    events = []
    original_body = repo._read_source_body

    def body(connection, metadata):
        events.append(("body", metadata.reference.source_id))
        return original_body(connection, metadata)

    monkeypatch.setattr(repo, "_read_source_body", body)

    def wrap(name, original):
        async def store(*args, **kwargs):
            events.append(("store", name))
            return await original(*args, **kwargs)
        return store

    for name in ("authorized_metadata", "read_sources"):
        monkeypatch.setattr(repo, name, wrap(name, getattr(repo, name)))
    for loader in loaders:
        policy = loader.reader.policy

        async def evaluate(policy_request, _original=policy.evaluate):
            events.append(("policy", policy_request.action))
            return await _original(policy_request)
        monkeypatch.setattr(policy, "evaluate", evaluate)
    return events


def reads(events):
    return [value for kind, value in events if kind == "body"]


def kinds(events):
    return [kind for kind, _ in events]


def dump(result: LoadedContext) -> str:
    return "\n".join((result.manifest.model_dump_json(), result.bundle.model_dump_json(),
                      repr(result.records), repr(result.mandatory)))


def outcomes(result, record):
    return [(r.stage, r.outcome, r.characters) for r in result.records
            if r.source_id == sid(record)]


class SpyRanker:
    """Stand-in for a model/embedding/reranker sink; records what it could observe."""

    def __init__(self, pick):
        self.pick, self.calls, self.events = pick, [], []

    async def rank(self, goal, items):
        self.calls.append({"goal": goal, "items": items, "seen": kinds(self.events)})
        self.events.append(("ranker", len(items)))
        return self.pick(items)


# AC1 -----------------------------------------------------------------------------------

async def test_l0_manifest_then_existing_l1_then_only_uncovered_l2_bodies(kb, monkeypatch):
    repo, service = kb
    a = await service.write(note("a", "needle alpha fact"), OWNER)
    b_text = "needle 베타 사실 한글"
    b = await service.write(note("b", b_text), OWNER)
    unrelated = await service.write(note("u", "weather only", title="unrelated"), OWNER)
    foreign = await service.write(note("f", "needle FOREIGN_BODY_CANARY",
                                       title="needle FOREIGN_TITLE_CANARY"), OTHER)
    summary = await derive(repo, "s", "needle summary of alpha", (await ref(repo, a),))
    a_ref = await ref(repo, a)
    loader = make_loader(repo)
    events = spy(monkeypatch, repo, loader)

    result = await loader.load(request(), instructions=("Answer in Korean.",),
                               rules=("Owner-only preview.",))

    # L0 is metadata only and lists only authorized, query-relevant sources.
    assert {i.source_id for i in result.manifest.items} == {sid(a), sid(b), sid(summary)}
    assert all(i.excerpt == "" for i in result.manifest.items)
    assert result.manifest.loaded_characters == 0
    # The existing L1 summary covers A, so only the uncovered B body is read at L2.
    assert reads(events) == [sid(summary), sid(b)]
    assert [(i.source_id, i.level) for i in result.bundle.items] == [
        (sid(summary), ContextLevel.L1), (sid(b), ContextLevel.L2)]
    s_item = result.bundle.items[0]
    assert s_item.epistemic_state == "tentative"  # never promoted to a cited fact
    assert s_item.parents == (a_ref,)
    assert outcomes(result, a) == [(ContextLevel.L0, "listed", 0),
                                   (ContextLevel.L2, "covered_by_summary", 0)]
    assert outcomes(result, b) == [(ContextLevel.L0, "listed", 0),
                                   (ContextLevel.L2, "loaded", len(b_text))]
    assert len(b_text) != len(b_text.encode())  # the unit is characters, not UTF-8 bytes
    assert outcomes(result, summary)[-1] == (ContextLevel.L1, "loaded",
                                             len("needle summary of alpha"))
    assert not outcomes(result, unrelated) and not outcomes(result, foreign)
    assert all(r.characters == r.stored_characters for r in result.records
               if r.outcome == "loaded")
    # Measured totals: exact character counts, no token estimate.
    assert result.bundle.loaded_characters == sum(r.characters for r in result.records)
    assert result.bundle.loaded_characters == len("needle summary of alpha") + len(b_text)
    assert result.bundle.measured_tokens is None and result.manifest.measured_tokens is None
    assert result.summaries == "used" and result.reason is None and not result.insufficient
    assert "CANARY" not in dump(result)
    # The KB holds 5 sources; only the 2 selected bodies were read (no whole-KB injection).
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT count(*) FROM kb_sources").fetchone()[0] == 5


async def test_without_summaries_reports_none_and_reads_only_bodies_that_fit(kb, monkeypatch):
    repo, service = kb
    small = await service.write(note("small", "needle " + "x" * 20), OWNER)
    large = await service.write(note("large", "needle " + "y" * 500), OWNER)
    loader = make_loader(repo)
    events = spy(monkeypatch, repo, loader)
    base = await loader.load(request(max_characters=100_000), depth=ContextLevel.L0)
    # depth=L0 returns the manifest only and reads zero bodies.
    assert reads(events) == [] and base.bundle.items == () and not base.insufficient
    assert len(base.manifest.items) == 2 and base.summaries == "not_requested"

    budget = base.mandatory_characters + 100
    result = await loader.load(request(max_characters=budget))
    assert result.summaries == "none_available"  # explicitly reported, nothing fabricated
    assert reads(events) == [sid(small)]  # the oversized body is never read
    assert outcomes(result, large)[-1] == (ContextLevel.L2, "skipped_budget", 0)
    assert {r.stored_characters for r in result.records if r.source_id == sid(large)} == {507}
    assert [i.source_id for i in result.bundle.items] == [sid(small)]
    assert result.total_characters <= budget and not result.insufficient


async def test_explicit_selection_is_not_replaced_and_stale_selection_fails_closed(
        kb, monkeypatch):
    repo, service = kb
    a = await service.write(note("a", "needle " + "a" * 200), OWNER)
    a_ref = await ref(repo, a)
    summary = await derive(repo, "s", "needle short summary", (a_ref,))
    s_ref = await ref(repo, summary)
    loader = make_loader(repo)
    events = spy(monkeypatch, repo, loader)

    chosen = await loader.load(request(selected_sources=(a_ref, s_ref)))
    assert reads(events) == [sid(summary), sid(a)]  # explicit A is loaded despite coverage
    assert {i.source_id for i in chosen.bundle.items} == {sid(a), sid(summary)}

    events.clear()
    tight = chosen.mandatory_characters + len("needle short summary") + 10
    partial = await loader.load(request(selected_sources=(a_ref, s_ref), max_characters=tight))
    assert reads(events) == [sid(summary)]
    assert partial.insufficient and partial.reason == "selection_incomplete"

    await service.write(note("a", "needle changed", revision="r2",
                             expected=a_ref.source_revision), OWNER)
    events.clear()
    with pytest.raises(RfaError):
        await loader.load(request(selected_sources=(a_ref,)))
    assert reads(events) == [] and "policy" not in kinds(events)


# AC2 -----------------------------------------------------------------------------------

async def test_mandatory_segments_are_reserved_and_tight_budget_is_insufficient(
        kb, monkeypatch):
    repo, service = kb
    source = await service.write(note("a", "needle " + "z" * 43), OWNER)  # 50 characters
    loader = make_loader(repo)
    events = spy(monkeypatch, repo, loader)
    instruction, rule = "I" * 30, "R" * 20
    mandatory = len(instruction) + len(GOAL) + len(rule) + len(UNTRUSTED_CONTEXT_RULE)
    expected_texts = [instruction, GOAL, rule, UNTRUSTED_CONTEXT_RULE]

    async def load(budget):
        return await loader.load(request(max_characters=budget), instructions=(instruction,),
                                 rules=(rule,))

    below = await load(mandatory - 1)
    assert below.reason == "mandatory_exceeds_budget" and below.insufficient
    assert [s.text for s in below.mandatory] == expected_texts  # never truncated or dropped
    assert [s.kind for s in below.mandatory] == [
        "instruction", "goal", "permission_rule", "permission_rule"]
    assert events == []  # no store, policy or body access at all

    tight = await load(mandatory + 10)
    assert tight.insufficient and tight.reason == "budget_exhausted"
    assert [s.text for s in tight.mandatory] == expected_texts
    assert reads(events) == [] and tight.bundle.loaded_characters == 0
    assert outcomes(tight, source)[-1] == (ContextLevel.L2, "skipped_budget", 0)

    exact = await load(mandatory + 50)
    assert reads(events) == [sid(source)] and not exact.insufficient
    assert exact.mandatory_characters == mandatory
    assert exact.total_characters == exact.budget_characters == mandatory + 50


async def test_authorization_precedes_ranker_and_ranker_cannot_add_sources(kb, monkeypatch):
    repo, service = kb
    a = await service.write(note("a", "needle alpha"), OWNER)
    b = await service.write(note("b", "needle beta"), OWNER)
    await service.write(note("f", "needle FOREIGN_CANARY", title="needle FOREIGN_CANARY"), OTHER)
    ranker = SpyRanker(lambda items: ["forged-source", sid(b), 42, sid(b)])
    loader = make_loader(repo, ranker=ranker)
    events = ranker.events = spy(monkeypatch, repo, loader)

    result = await loader.load(request())

    assert len(ranker.calls) == 1
    call = ranker.calls[0]
    assert "policy" in call["seen"] and "body" not in call["seen"]  # authorized, no body yet
    assert call["goal"] == GOAL and all(i.excerpt == "" for i in call["items"])
    assert {i.source_id for i in call["items"]} == {sid(a), sid(b)}
    assert "FOREIGN_CANARY" not in repr(call["items"])
    assert reads(events) == [sid(b)]
    assert [i.source_id for i in result.bundle.items] == [sid(b)]
    assert outcomes(result, a)[-1] == (ContextLevel.L2, "not_selected", 0)
    assert "forged-source" not in dump(result)
    order = kinds(events)
    # The body read gets its own stage authorization after ranking.
    assert "policy" in order[order.index("ranker"):order.index("body")]


@pytest.mark.parametrize("damage", ["endpoint", "principal", "role", "target", "issuer"])
async def test_unbound_or_non_local_requests_fail_before_store_policy_or_ranker(
        kb, monkeypatch, damage):
    repo, service = kb
    await service.write(note("a", "needle alpha"), OWNER)
    ranker = SpyRanker(lambda items: [i.source_id for i in items])
    options = {"endpoint": "cloud-model"} if damage == "endpoint" else {}
    loader = make_loader(repo, ranker=ranker, issuer=damage != "issuer", **options)
    events = ranker.events = spy(monkeypatch, repo, loader)
    changes = {"endpoint": {"endpoint_id": "cloud-model"}, "principal": {"principal": OTHER},
               "role": {"role": "result_analyst"}, "target": {"target": "public"},
               "issuer": {}}[damage]
    with pytest.raises(RfaError) as error:
        await loader.load(request(**changes))
    assert error.value.code == ("not_implemented" if damage == "issuer" else "policy_denied")
    assert events == [] and ranker.calls == []


# AC3 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("mutation", ["revision", "delete", "acl", "policy"])
async def test_multi_parent_change_invalidates_summary_reuse_and_old_receipts(
        kb, monkeypatch, mutation):
    repo, service = kb
    a = await service.write(note("a", "needle alpha"), OWNER)
    b = await service.write(note("b", "needle beta"), OWNER)
    b_rev = b.document.source_revision
    summary = await derive(repo, "s", "needle SUMMARY_CANARY of both",
                           (await ref(repo, a), await ref(repo, b)))
    s_ref = await ref(repo, summary)
    loader = make_loader(repo)
    events = spy(monkeypatch, repo, loader)
    first = await loader.load(request())
    assert reads(events) == [sid(summary)]  # both parents covered by the summary
    s_item = first.bundle.items[0]
    assert s_item.source_id == sid(summary) and len(s_item.parents) == 2

    if mutation == "delete":
        await service.delete(sid(b), KnowledgeDelete(expected_revision=b_rev,
                                                     mutation_id="delete-b"), OWNER)
    elif mutation == "policy":
        await repo.write_knowledge(note("b", "needle beta", revision="r2", expected=b_rev),
                                   OWNER, policy_version="local-v2")
    else:
        acl = {"audience": "company", "company_id": "company-a"} if mutation == "acl" else None
        await service.write(note("b", "needle beta v2", revision="r2", expected=b_rev, acl=acl),
                            OWNER)
    events.clear()
    second = await loader.load(request(), previous=first.bundle)

    assert sid(summary) not in {i.source_id for i in second.manifest.items}
    assert sid(summary) not in {i.source_id for i in second.bundle.items}
    assert second.refused_previous == 1 and "SUMMARY_CANARY" not in dump(second)
    assert sid(summary) not in reads(events)
    expected = {sid(a)} | ({sid(b)} if mutation in {"revision", "acl"} else set())
    assert set(reads(events)) == expected and second.summaries == "none_available"
    # The old stage receipts (the reader's process cache) are refused before any body read.
    events.clear()
    stale = request(level="L1", selected_sources=(s_ref,), policies=s_item.policies)
    with pytest.raises(RfaError):
        await loader.reader.load_context(stale)
    assert reads(events) == []


async def test_valid_previous_summary_is_reused_without_reads_and_tampering_is_refused(
        kb, monkeypatch):
    repo, service = kb
    a = await service.write(note("a", "needle alpha"), OWNER)
    summary = await derive(repo, "s", "needle summary of alpha", (await ref(repo, a),))
    s_ref = await ref(repo, summary)
    loader = make_loader(repo)
    events = spy(monkeypatch, repo, loader)
    first = await loader.load(request())
    assert reads(events) == [sid(summary)]

    events.clear()
    again = await loader.load(request(), previous=first.bundle)
    assert reads(events) == [] and again.refused_previous == 0
    assert outcomes(again, summary)[-1] == (ContextLevel.L1, "reused",
                                            len("needle summary of alpha"))
    reused = again.bundle.items[0]
    assert reused.excerpt == first.bundle.items[0].excerpt
    decision = loader.reader.decision(reused.policies.read_decision_id)  # fresh live receipt
    assert decision.allowed and s_ref in decision.source_refs

    forged_item = first.bundle.items[0].model_copy(update={"excerpt": "needle FORGED_CANARY"})
    forged = first.bundle.model_copy(update={"items": (forged_item,)})
    events.clear()
    third = await loader.load(request(), previous=forged)
    assert third.refused_previous == 1 and reads(events) == [sid(summary)]
    assert third.bundle.items[0].excerpt == "needle summary of alpha"
    assert "FORGED_CANARY" not in dump(third)


async def test_public_target_never_receives_private_titles_summaries_or_relations(
        kb, monkeypatch):
    repo, service = kb
    pub = await service.write(note("pub", "needle public fact", audience="public"), OWNER)
    priv = await service.write(note("priv", "needle PRIVATE_BODY_CANARY",
                                    title="needle PRIVATE_TITLE_CANARY"), OWNER)
    pub_ref, priv_ref = await ref(repo, pub), await ref(repo, priv)
    relation = await derive(repo, "rel", "needle RELATION_SUMMARY_CANARY", (pub_ref, priv_ref),
                            title="needle RELATION_TITLE_CANARY", audience="public")
    pub_summary = await derive(repo, "pubsum", "needle public summary", (pub_ref,),
                               audience="public")
    owner_loader = make_loader(repo)
    public_loader = make_loader(repo, Audience.PUBLIC)
    events = spy(monkeypatch, repo, owner_loader, public_loader)

    owner = await owner_loader.load(request())
    # Sanity: the owner context really contains the private relation summary.
    assert "RELATION_SUMMARY_CANARY" in owner.bundle.model_dump_json()

    events.clear()
    public = await public_loader.load(request("public"), previous=owner.bundle)
    assert {i.source_id for i in public.manifest.items} == {sid(pub), sid(pub_summary)}
    text = dump(public)
    for secret in ("CANARY", sid(priv), sid(relation), priv_ref.source_revision):
        assert secret not in text
    assert public.refused_previous == 1  # the owner-only relation summary is not reused
    assert outcomes(public, pub_summary)[-1][:2] == (ContextLevel.L1, "reused")
    assert reads(events) == [] and not public.insufficient
    assert all(i.audience == Audience.PUBLIC
               and all(p.audience == Audience.PUBLIC for p in i.parents)
               for i in public.bundle.items)

    events.clear()
    with pytest.raises(RfaError):
        await public_loader.load(request("public", selected_sources=(priv_ref,)))
    assert reads(events) == []


# AC4 -----------------------------------------------------------------------------------

async def test_loader_is_read_only_and_returns_port_dtos_via_container(
        container, principal, monkeypatch):
    repo = container.repository
    source = await container.knowledge.write(note("a", "needle alpha fact"), principal)
    metadata = await repo.authorized_metadata(DomainId.TRIV3, principal)
    a_ref = next(m.reference for m in metadata if m.reference.source_id == sid(source))
    summary = await repo.write_derived_knowledge(
        note("s", "needle derived summary"), principal, parents=(a_ref,),
        policy_version=container.policy.policy_version, epistemic_state="tentative")

    def counts():
        with sqlite3.connect(repo.path) as db:
            return [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("kb_documents", "kb_revision_context", "kb_source_revisions")]

    before = counts()

    async def forbidden(*args, **kwargs):
        raise AssertionError("the loader must not write, summarize, copy or call a model")

    for name in ("write_knowledge", "write_derived_knowledge", "upsert_documents"):
        monkeypatch.setattr(repo, name, forbidden)
    monkeypatch.setattr(container.model, "generate", forbidden)
    bound = BoundAccess(lambda: principal, "source-scout", "source_scout", DomainId.TRIV3,
                        DraftTarget(audience=Audience.OWNER), "local-model")
    loader = ContextLoader(repo, container.context_reader(bound))
    result = await loader.load(request(principal=principal))

    assert counts() == before
    assert isinstance(result.bundle, ContextBundle) and isinstance(result.manifest, ContextBundle)
    assert all(isinstance(i, ContextItem) for i in result.bundle.items)
    assert ContextBundle.model_validate_json(result.bundle.model_dump_json()) == result.bundle
    assert ContextBundle.model_validate_json(result.manifest.model_dump_json()) == result.manifest
    assert [(i.source_id, i.epistemic_state) for i in result.bundle.items] == [
        (sid(summary), "tentative")]
    assert result.bundle.adapter == "staged-context-v1" and not result.bundle.simulated
    assert result.bundle.policy_version == container.policy.policy_version
