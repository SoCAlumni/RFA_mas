"""Integrity checks for the pinned E2E fixture pack (fixtures/e2e).

These tests check fixture data and its fit with current contracts only. They are not
E2E-01..10 scenario results; scenario replays belong to tests/e2e/test_rfa_scenarios.py.
"""

from __future__ import annotations

import re

from fixture_pack import (
    export_batches,
    file_digest,
    filler_digest,
    filler_payloads,
    golden,
    note_writes,
    pack_files,
    payload_revision,
    payload_text,
    payloads,
    personas,
    read_json,
    set_members,
    sources,
)

from rfa_mas.contracts import (
    FeedbackCategory,
    KnowledgeExport,
    KnowledgeWrite,
    ScheduleSpec,
)

CANARY = re.compile(r"SYNTHETIC_PRIVATE_CANARY_[A-Z0-9_]+")
FACT_TOKENS = (
    "10.0ms",
    "8.2ms",
    "81.0%",
    "80.8%",
    "6.0ms",
    "2026-10-20",
    "2026-10-12",
    "2026-10-27",
    "#17",
)


def test_manifest_pins_every_pack_file():
    manifest = read_json("manifest.json")
    assert sorted(manifest["files"]) == pack_files()
    changed = [name for name, digest in manifest["files"].items() if file_digest(name) != digest]
    assert changed == []


def test_payloads_resolve_one_to_one_and_match_scope_and_revision():
    index, scope_acl = sources(), read_json("sources.json")["scope_acl"]
    resolved = payloads()
    assert set(resolved) == set(index)
    for fid, (channel, payload) in resolved.items():
        entry = index[fid]
        assert channel == entry["channel"], fid
        assert payload["acl"] == scope_acl[entry["scope"]], fid
        assert payload_revision(channel, payload) == entry["provider_revision"], fid
        assert payload_text(channel, payload).startswith("[합성 "), fid


def test_inputs_variants_and_schedule_validate_against_current_contracts():
    assert len(note_writes()) + sum(len(ids) for ids, _ in export_batches()) == len(sources())
    assert all(write.synthetic for _, write in note_writes())
    variants = read_json("variants.json")["variants"]
    for name in ("R01-v2", "R05-closed"):
        batch = KnowledgeExport.model_validate(variants[name]["export"])
        base = payloads()[variants[name]["base"]][1]
        id_key = "id" if batch.provider == "confluence" else "number"
        assert batch.rows[0][id_key] == base[id_key]
        assert payload_revision(batch.provider, batch.rows[0]) != payload_revision(
            batch.provider, base
        )
    attack = variants["R06-attack"]
    for item in attack["attacks"]:
        if item["injection_point"] == "document_body":
            write = KnowledgeWrite.model_validate(item["request"])
            assert write.provenance.external_id == sources()["R06"]["external_id"]
            assert item["payload"] in write.content
        else:
            assert item["tool_result"]["value"] == item["payload"]
    assert {a["category"] for a in attack["attacks"]} == {
        "approval_bypass",
        "private_note_exfiltration",
        "fake_higher_instruction",
        "tool_argument_poisoning",
    }
    KnowledgeWrite.model_validate(attack["security_explainer"]["request"])
    assert FeedbackCategory(variants["M01"]["category"]) == FeedbackCategory.STYLE_PREFERENCE
    schedule = read_json("scenarios.json")["scenarios"]["E2E-04"]["schedule"]
    assert ScheduleSpec.model_validate(schedule).misfire_policy == "latest_once"


def test_small_set_is_core_r01_to_r10_plus_nine_irrelevant_documents():
    assert len(set_members("small20")) == 20
    assert set(set_members("core")) == {f"R{n:02d}" for n in range(1, 10)} | {"R10-PA", "R10-PB"}
    assert len(set_members("irrelevant")) == 9
    assert not set(set_members("small20")) & {"D01", "R04-run2", "R05-dup", "I15"}


def test_source_text_carries_indexed_facts_and_golden_arithmetic_is_right():
    index, resolved = sources(), payloads()
    text = {fid: payload_text(*resolved[fid]) for fid in resolved}
    for fid in ("R03", "R04", "R04-run2"):
        facts = index[fid]["facts"]
        assert f"지연: {facts['latency_ms']:.1f}ms" in text[fid]
        assert f"정확도: {facts['accuracy_pct']:.1f}%" in text[fid]
        assert facts["environment"] in text[fid]
    a, b = index["R03"]["facts"], index["R04"]["facts"]
    reduction = round((a["latency_ms"] - b["latency_ms"]) / a["latency_ms"] * 100)
    delta = round(b["accuracy_pct"] - a["accuracy_pct"], 1)
    questions = {q["question_id"]: q for q in golden()}
    assert f"{reduction}%" in questions["G01"]["expected_facts"] and reduction == 18
    assert f"{abs(delta)}%p" in questions["G02"]["expected_facts"] and delta == -0.2
    calc = read_json("scenarios.json")["scenarios"]["E2E-03"]["expected_cpu_calculation"]
    assert calc == {"latency_reduction_pct": float(reduction), "accuracy_delta_pp": delta}
    assert index["R01"]["facts"]["official_sdk_release_date"] in text["R01"]
    assert index["R02"]["facts"]["internal_validation_target_date"] in text["R02"]
    assert "6.0ms" in text["R06"] and "가설" in text["R06"] and "검증 전" in text["R06"]
    assert "상태: open" in text["R05"] and "마감: 2026-10-02" in text["R05"]
    assert "상태: closed" in text["I15"]
    assert text["D01"] == text["R09"] and index["D01"]["same_body_as"] == "R09"
    v2 = read_json("variants.json")["variants"]["R01-v2"]["export"]["rows"][0]["body"]
    assert "2026-10-27" in v2 and "2026-10-20" not in v2


def test_canaries_exist_only_in_owner_private_sources():
    index, resolved = sources(), payloads()
    for fid, (channel, payload) in resolved.items():
        found = set(CANARY.findall(payload_text(channel, payload)))
        if index[fid]["scope"] == "owner_private":
            assert found and found == set(index[fid]["canaries"]), fid
        else:
            assert not found and not index[fid]["canaries"], fid
    variants = read_json("variants.json")["variants"]
    shared_texts = [q["question"] for q in golden()]
    shared_texts += [a["payload"] for a in variants["R06-attack"]["attacks"]]
    shared_texts.append(variants["R06-attack"]["security_explainer"]["request"]["content"])
    shared_texts += [variants[n]["export"]["rows"][0]["body"] for n in ("R01-v2", "R05-closed")]
    shared_texts.append(read_json("personas.json")["role_claim_attack"]["text"])
    assert [t for t in shared_texts if CANARY.search(t)] == []


def test_access_matrix_follows_scope_labels_for_each_persona():
    index, people = sources(), personas()
    scope_acl = read_json("sources.json")["scope_acl"]
    matrix = read_json("access_matrix.json")

    def readable(persona_id: str, scope: str) -> bool:
        acl, who = scope_acl[scope], people[persona_id]
        if acl["audience"] == "public":
            return True
        if acl["audience"] == "private":
            return persona_id == "owner"
        same_company = who.authenticated and who.company_id == acl["company_id"]
        if acl["audience"] == "company":
            return same_company
        return same_company and set(acl["memberships"]) <= who.business_units

    for persona_id in people:
        expected = [fid for fid, e in index.items() if readable(persona_id, e["scope"])]
        assert matrix["read"][persona_id] == expected, persona_id
    assert matrix["public_shareable"] == [f for f, e in index.items() if e["scope"] == "public"]
    assert matrix["never_auto_share"] == ["R07", "R08"]
    assert matrix["read"]["external"] == ["R01", "R10-PA", "R10-PB"]


def test_golden_questions_have_valid_recall_denominators():
    questions, matrix = golden(), read_json("access_matrix.json")
    assert [q["question_id"] for q in questions] == [f"G{n:02d}" for n in range(1, 21)]
    small = set(set_members("small20"))
    for q in questions:
        required = q["required_sources"]
        assert q["answerable"] is bool(required) is bool(q["expected_facts"]), q["question_id"]
        assert len(set(required)) == len(required) and set(required) <= small
        assert set(required) <= set(matrix["read"][q["persona_id"]])
        if not q["answerable"]:
            assert q["expected_conclusion"] == "insufficient_evidence"
    unanswerable = [q["question_id"] for q in questions if not q["answerable"]]
    scenario = read_json("scenarios.json")["scenarios"]["E2E-02"]
    assert unanswerable == scenario["unanswerable_question_ids"] and len(unanswerable) >= 3


def test_load_corpus_is_deterministic_pinned_and_free_of_facts_and_canaries():
    spec = read_json("load_corpus.json")
    fillers = filler_payloads(spec)
    assert len(fillers) == spec["filler_count"]
    assert spec["filler_count"] + len(set_members(spec["base_set"])) == spec["total_documents"]
    assert filler_digest(fillers) == spec["filler_sha256"]
    ids = [item["provenance"]["external_id"] for item in fillers]
    assert len(set(ids)) == len(ids)
    assert not set(ids) & {entry["external_id"] for entry in sources().values()}
    low, high = spec["document_chars"]["min"], spec["document_chars"]["max"]
    assert spec["chunk_rule"]["max_chars"] >= high
    for item in fillers:
        assert low <= len(item["content"]) <= high
        assert not any(token in item["content"] for token in FACT_TOKENS)
        assert not CANARY.search(item["content"])
    for item in (fillers[0], fillers[-1]):
        KnowledgeWrite.model_validate(item)


async def test_small_set_ingests_through_current_knowledge_service(container):
    owner = personas()["owner"]
    expected_audience = {}
    for fid, write in note_writes("small20"):
        record = await container.knowledge.write(write, owner)
        expected_audience[write.provenance.external_id] = write.acl.audience
        assert record.document.synthetic, fid
    for ids, batch in export_batches("small20"):
        result = await container.knowledge.import_export(batch, owner)
        assert [row.status for row in result.rows] == ["accepted"] * len(ids)
        for row in batch.rows:
            key = str(row["number"] if batch.provider == "github_issue" else row["id"])
            expected_audience[key] = row["acl"]["audience"]
    records = await container.knowledge.list(owner)
    assert len(records) == 20
    stored = {r.provenance.external_id: r.document.audience for r in records}
    assert stored == expected_audience


def test_materials_table_matches_payloads_scopes_personas_and_load_spec():
    materials = read_json("materials_v1.json")
    index, resolved, scope_acl = sources(), payloads(), read_json("sources.json")["scope_acl"]
    assert [m["id"] for m in materials["materials"]] == sorted(set_members("core"))
    for item in materials["materials"]:
        channel, payload = resolved[item["id"]]
        text = payload_text(channel, payload)
        mapped = materials["scope_mapping"][item["doc_scope"]]
        assert mapped["scope"] == index[item["id"]]["scope"], item["id"]
        assert payload["acl"] == mapped["acl"] == scope_acl[mapped["scope"]], item["id"]
        assert all(fact in text for fact in item["facts"]), item["id"]
        assert [c for c in item["canaries"] if c in text] == item["canaries"], item["id"]
        assert bool(item["canaries"]) is (mapped["scope"] == "owner_private"), item["id"]
    variants = read_json("variants.json")["variants"]
    for item in materials["variants"]:
        assert item["id"] in variants
        if "facts" in item:
            body = variants[item["id"]]["export"]["rows"][0]["body"]
            assert all(fact in body for fact in item["facts"]), item["id"]
            assert not any(fact in body for fact in item["absent_facts"]), item["id"]
    derived = materials["derived_expectations"]
    a, b = index["R03"]["facts"], index["R04"]["facts"]
    change = round((b["latency_ms"] - a["latency_ms"]) / a["latency_ms"] * 100, 1)
    assert change == derived["latency_change_pct"] == -derived["latency_reduction_pct"]
    assert round(b["accuracy_pct"] - a["accuracy_pct"], 1) == derived["accuracy_delta_pp"]
    assert set(materials["personas"]["ids"]) == set(personas())
    load, spec = materials["sets"]["load1000"], read_json("load_corpus.json")
    assert (load["seed"], load["total_documents"], load["generated_at_test_time"]) == (
        spec["seed"],
        spec["total_documents"],
        spec["filler_count"],
    )
    assert load["committed_documents"] == len(set_members("small20")) == 20
