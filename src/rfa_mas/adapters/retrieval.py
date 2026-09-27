"""Real local lexical search and deliberately bounded, local-only context reader."""
import math
import re
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from rfa_mas.application.source_access import BoundAccess
from rfa_mas.contracts import (
    ContextBundle, ContextItem, ContextLevel, ContextRequest, EvidenceBundle, EvidenceItem,
    PolicyBindings, PolicyDecisionV11, PolicyRequest, RetrievalRequest, new_id,
)
from rfa_mas.errors import RfaError


# -- P1-001D: deterministic Korean-aware lexical ranking ---------------------------------
# Query-side analysis only; documents are matched by substring inside SQLite, so a doc
# spelling like "지연:" or "지연이" both contain the stripped query term "지연".
# Scripts are split (Latin/digit vs Hangul) so "a의", "17의", "SDK가" lose their particle.
_TOKEN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*|[가-힣]+")
_PARTICLES = tuple(sorted({
    "으로부터", "에게서", "으로서", "으로써", "에서는", "에서도", "에게는", "까지는", "부터는",
    "이라고", "라고", "이랑", "에게", "에서", "으로", "까지", "부터", "보다", "처럼", "마다",
    "조차", "마저", "하고", "이나", "이란", "이며", "이고", "과", "와", "은", "는", "이", "가",
    "을", "를", "의", "에", "도", "만", "로", "랑",
}, key=lambda p: (-len(p), p)))
_PARTICLE_SET = frozenset(_PARTICLES)
# Generic request words/question words: never evidence about a source.
_STOPWORDS = frozenset({
    "알려", "알려줘", "알려주세요", "줘", "주세요", "해줘", "뭐야", "언제야", "얼마야", "얼마나",
    "어떻게", "거야", "무엇", "어디", "언제", "누구", "얼마", "좀", "보여줘", "말해줘", "설명해",
    "설명해줘", "요약해", "요약해줘", "정리해", "정리해줘",
})
BM25_K1 = 1.2
BM25_B = 0.75
# One SQL result column per term (SQLite allows 2000); a request may be 10,000 characters.
MAX_QUERY_TERMS = 128


def _strip_particle(word: str) -> str:
    for particle in _PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def lexical_terms(query: str) -> tuple[tuple[str, bool], ...]:
    """Sorted unique (term, boundary) pairs. boundary=True: whole-token match only.

    Hangul words drop one trailing particle and add character bigrams (the Lucene CJK
    bigram approach) so "담당자"/"마감일" still meet "담당"/"마감". Short Latin/digit
    tokens (<=2 chars, e.g. "a", "17") only match as whole tokens, never inside words.
    At most MAX_QUERY_TERMS terms are kept deterministically: whole words before bigrams.
    """
    terms: set[tuple[str, bool]] = set()
    bigrams: set[tuple[str, bool]] = set()
    for token in _TOKEN.findall(query.lower()):
        if token in _STOPWORDS or token in _PARTICLE_SET:
            continue
        if "가" <= token[0] <= "힣":
            word = _strip_particle(token)
            if len(word) < 2 or word in _STOPWORDS:
                continue
            terms.add((word, False))
            if len(word) >= 3:
                bigrams.update((word[i:i + 2], False) for i in range(len(word) - 1))
        else:
            terms.add((token, len(token) <= 2))
    kept = sorted(terms)[:MAX_QUERY_TERMS]
    kept += sorted(bigrams - terms)[: MAX_QUERY_TERMS - len(kept)]
    return tuple(sorted(kept))


# -- P1-001E: relevance gate after authorization ------------------------------------------
# A query is answered "insufficient" instead of by a partial-term match when its distinctive
# terms are not covered by the caller's AUTHORIZED documents (never by documents the caller
# cannot read). Rules (RELEVANCE_RULES_VERSION):
#   R1 none covered: the query has distinctive terms and not one of them is covered.
#   R2 unknown qualifier: an uncovered Hangul word that MODIFIES the covered Latin entity token
#      right after it ("경쟁사 SDK", "방식의 GPU"): the query narrows a known named subject to
#      something the KB never mentions. Only a bare word or a genitive "의" modifies the next
#      token; any other particle ("일정과 benchmark", "일정을 SDK") ends the phrase. Uncovered
#      Lowercase Latin words are exempt (English synonyms such as "latency" for "지연"), and a following
#      Korean word may be a verb-like noun ("공개").
#   R3 unknown explicit identifier: a 3+ character ALL-CAPS token (including hyphenated
#      identifiers) is absent from authorized documents. Generic overlap cannot stand in
#      for evidence about that named subject. This is a conservative lexical heuristic,
#      not general named-entity recognition; lowercase synonyms still use R1/R2.
# Distinctive: Latin/digit tokens of 3+ characters; particle-stripped Hangul words of 2+
# characters that are not request, relational or deictic words and do not end in a
# predicate/connective ending. Covered: the word, or for a 3+ character Hangul word any of
# its bigrams (the P1-001D matching rule), occurs in an authorized document.
RELEVANCE_RULES_VERSION = "relevance-gate-v2"
_EXPLICIT_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]*(?:[-_.][A-Z0-9]+)*(?![A-Za-z0-9])")
_GENERIC = frozenset({
    # request/task words and relational modifiers: never the subject of a question
    "요약", "답변", "초안", "작성", "정리", "설명", "조사", "비교", "검토", "관련", "대한", "관한",
    "위한", "통한", "대해", "실제", "전체", "일반", "기타", "진행", "상황", "현황", "정보",
    # deictic and question words: refer to the clock or the asker, not to a source
    "오늘", "내일", "어제", "지금", "현재", "이번", "최근", "요즘", "며칠", "무슨", "어느", "어떤",
    "언제", "누가", "뭐가",
})
_PREDICATE_ENDINGS = tuple("어아야요다니까지해돼줘고게면한된할될는은던인나냐래네죠며서혀도하")
_MODIFIER_SUFFIXES = ("", "의")  # bare noun or genitive: attached to the following token


def query_words(query: str) -> tuple[tuple[str, bool, bool, bool], ...]:
    """(word, is_hangul, distinctive, modifies_next) per query token, in query order."""
    words = []
    for token in _TOKEN.findall(query.lower()):
        if "가" <= token[0] <= "힣":
            word = _strip_particle(token)
            distinctive = (len(word) >= 2 and word not in _STOPWORDS and word not in _GENERIC
                           and word not in _PARTICLE_SET
                           and not word.endswith(_PREDICATE_ENDINGS))
            words.append((word, True, distinctive, token[len(word):] in _MODIFIER_SUFFIXES))
        else:
            words.append((token, False, len(token) >= 3 and token not in _STOPWORDS, False))
    return tuple(words)


def relevance_insufficient(query: str, document_frequency: dict[str, int]) -> bool:
    """True when R1, R2 or R3 holds; counts come only from authorized documents.

    A term missing from the map (e.g. beyond MAX_QUERY_TERMS) counts as covered, so the gate
    only ever withholds on positive evidence of absence.
    """
    def covered(word: str, hangul: bool) -> bool:
        if word not in document_frequency:
            return True
        if document_frequency[word] > 0:
            return True
        return hangul and len(word) >= 3 and any(
            document_frequency.get(word[i:i + 2], 1) > 0 for i in range(len(word) - 1))

    if any(len(token) >= 3 and not covered(token.lower(), False)
           for token in _EXPLICIT_IDENTIFIER.findall(query)):
        return True  # R3: explicit subject absent, even if generic query words match
    words = query_words(query)
    marks = [(word, hangul, distinctive and covered(word, hangul), distinctive, modifies)
             for word, hangul, distinctive, modifies in words]
    distinctive = [m for m in marks if m[3]]
    if distinctive and not any(m[2] for m in distinctive):
        return True  # R1
    return any(  # R2
        hangul and is_distinctive and modifies and not is_covered and not after[1] and after[2]
        for (_, hangul, is_covered, is_distinctive, modifies), after
        in zip(marks, marks[1:], strict=False)
    )


def bm25_scores(rows, term_count: int) -> dict[str, float]:
    """rows: (source_id, doc_length, tf_1..tf_n) numbers only; returns positive scores."""
    if not rows:
        return {}
    total = len(rows)
    average = sum(row[1] for row in rows) / total or 1.0
    frequencies = [sum(1 for row in rows if row[2 + i] > 0) for i in range(term_count)]
    idf = [math.log(1 + (total - df + 0.5) / (df + 0.5)) for df in frequencies]
    scores = {}
    for row in rows:
        norm = BM25_K1 * (1 - BM25_B + BM25_B * row[1] / average)
        score = sum(
            idf[i] * row[2 + i] * (BM25_K1 + 1) / (row[2 + i] + norm)
            for i in range(term_count)
            if row[2 + i] > 0
        )
        if score > 0:
            scores[row[0]] = score
    return scores



class LocalRetrieval:
    adapter_name = "local-lexical-v1"
    simulated = False

    def __init__(self, repository, *, policy_version):
        self.repository, self.policy_version = repository, policy_version

    async def search(self, request: RetrievalRequest) -> EvidenceBundle:
        request = RetrievalRequest.model_validate_json(request.model_dump_json(warnings=False), strict=True)
        metadata = await self.repository.authorized_metadata(request.domain_id,request.principal,
            audiences=request.allowed_audiences,policy_version=self.policy_version,
            query=request.query,limit=request.limit)
        reads = await self.repository.read_sources(request.domain_id,request.principal,
            [m.reference for m in metadata],audiences=request.allowed_audiences,
            policy_version=self.policy_version)
        items = tuple(EvidenceItem(**r.metadata.reference.model_dump(exclude={
            "schema_version","acl_revision","policy_version"}),
            excerpt=r.content,policy_version=self.policy_version) for r in reads)
        return EvidenceBundle(request_id=request.request_id,trace_id=request.trace_id,
            run_id=request.run_id,agent_id=request.agent_id,domain_id=request.domain_id,
            items=items,insufficient=not items,policy_version=self.policy_version,
            simulated=False,adapter=self.adapter_name)

    async def load_context(self, request):
        # Unbound graph/service callers cannot mint their own role/target identity.
        raise RfaError("policy_denied", "서버에 결합된 context reader가 필요합니다.")


class BoundContextReader:
    """Issued decisions are bounded process-local receipts, not approval authority.

    One reader is created by trusted composition for one server-owned descriptor.
    All live grants are resolved on each read; receipts never freeze those grants.
    """
    def __init__(self, repository, policy, bound: BoundAccess, *, issuer_supported=False,
                 clock=lambda: datetime.now(UTC), ttl_seconds=60, capacity=128):
        self.repository, self.policy, self.bound = repository, policy, bound
        self.issuer_supported, self.clock = issuer_supported, clock
        if not 0 < ttl_seconds <= 300 or not 1 <= capacity <= 1024:
            raise ValueError("bounded context receipt capacity/ttl required")
        self.ttl_seconds, self.capacity = ttl_seconds, capacity
        self._receipts = OrderedDict()

    def _request(self, request):
        try:
            request = ContextRequest.model_validate_json(request.model_dump_json(warnings=False), strict=True)
            principal = self.bound.identity(request)
        except (ValueError, AttributeError):
            raise RfaError("policy_denied", "context 요청이 유효하지 않습니다.") from None
        if not self.issuer_supported:
            raise RfaError("not_implemented", "선택 정책의 context 발급을 지원하지 않습니다.")
        return request, principal

    async def _metadata(self, request, principal):
        kw=dict(audiences=request.allowed_audiences,target=request.target.audience,
                endpoint=request.endpoint_id,policy_version=self.policy.policy_version)
        if request.selected_sources:
            return await self.repository.read_sources(request.domain_id,principal,
                request.selected_sources,metadata_only=True,**kw)
        return await self.repository.authorized_metadata(request.domain_id,principal,
            query=request.query,limit=request.limit,**kw)

    def _binding(self, request, principal, refs):
        return (principal.model_dump_json(),request.agent_id,request.role,request.domain_id,
                request.target.model_dump_json(),request.endpoint_id,self.policy.policy_version,
                tuple(r.model_dump_json() for r in refs))

    async def issue(self, request):
        request, principal = self._request(request)
        metadata = await self._metadata(request,principal)
        if not metadata:
            raise RfaError("policy_denied", "판정할 허용 근거가 충분하지 않습니다.")
        all_metadata = list(metadata)
        seen = {(m.reference.source_id,m.reference.source_revision) for m in metadata}
        # The repository rechecks the entire closure, including diamond revisions.
        for item in all_metadata:
            for parent in item.parents:
                key = (parent.source_id,parent.source_revision)
                if key not in seen:
                    seen.add(key)
                    if len(seen) > 128:
                        raise RfaError("policy_denied", "근거 범위를 초과했습니다.")
                    all_metadata.extend(await self.repository.read_sources(request.domain_id,
                        principal,(parent,),audiences=request.allowed_audiences,
                        target=request.target.audience,endpoint=request.endpoint_id,
                        policy_version=self.policy.policy_version,metadata_only=True))
        refs = tuple(m.reference for m in metadata)
        decisions=[]
        for action in ("read","share","egress"):
            for item in all_metadata:
                decision = await self.policy.evaluate(PolicyRequest(
                    request_id=request.request_id,trace_id=request.trace_id,run_id=request.run_id,
                    agent_id=request.agent_id,domain_id=request.domain_id,action=action,
                    resource_audience=item.reference.audience,resource_owner_id=item.owner_id,
                    resource_company_id=item.company_id,
                    resource_business_unit=next(iter(sorted(set(item.required_memberships)&principal.business_units)),None),
                    target_audience=request.target.audience,principal=principal))
                if not decision.allowed or decision.policy_version != self.policy.policy_version:
                    raise RfaError("policy_denied", "현재 정책으로 근거를 사용할 수 없습니다.")
            now=self.clock()
            # Local endpoint restriction is checked BEFORE any policy invocation.
            decisions.append(PolicyDecisionV11(request_id=request.request_id,trace_id=request.trace_id,
                run_id=request.run_id,agent_id=request.agent_id,domain_id=request.domain_id,
                allowed=True,code="allowed",safe_reason="현재 로컬 자료 범위를 확인했습니다.",
                policy_version=self.policy.policy_version,allowed_audiences=request.allowed_audiences,
                simulated=False,adapter="local-context-policy",decision_id=new_id("policy"),
                decision="allow",action=action,subject_id=principal.user_id,
                resource_id="context",recipient="local-preview",issued_at=now,
                expires_at=now+timedelta(seconds=self.ttl_seconds),
                source_refs=tuple(m.reference for m in all_metadata)))
        binding=self._binding(request,principal,refs)
        for decision in decisions:
            self._receipts[decision.decision_id]=(decision.model_copy(deep=True),binding)
            while len(self._receipts)>self.capacity:
                self._receipts.popitem(last=False)
        return PolicyBindings(read_decision_id=decisions[0].decision_id,
            share_decision_id=decisions[1].decision_id,egress_decision_id=decisions[2].decision_id)

    def decision(self, decision_id):
        value=self._receipts.get(decision_id)
        return value[0].model_copy(deep=True) if value else None

    async def load_context(self, request):
        request, principal = self._request(request)
        if request.policies is None:
            raise RfaError("policy_denied", "발급된 현재 자료 판정이 필요합니다.")
        metadata=await self._metadata(request,principal)  # Fresh project grants/current parents.
        refs=tuple(m.reference for m in metadata)
        binding=self._binding(request,principal,refs)
        ids=(request.policies.read_decision_id,request.policies.share_decision_id,
             request.policies.egress_decision_id)
        for action,identity in zip(("read","share","egress"),ids,strict=True):
            stored=self._receipts.get(identity)
            if (not stored or stored[1]!=binding or stored[0].action!=action
                or stored[0].expires_at<=self.clock() or not stored[0].allowed):
                raise RfaError("policy_denied", "현재 자료 판정이 유효하지 않습니다.")
        selected=metadata
        if request.level == ContextLevel.L1:
            selected=[m for m in metadata if m.parents]  # Existing derived summaries only.
        if request.level == ContextLevel.L0:
            contents=[(m,"") for m in selected]
        else:
            reads=await self.repository.read_sources(request.domain_id,principal,
                [m.reference for m in selected],audiences=request.allowed_audiences,
                target=request.target.audience,endpoint=request.endpoint_id,
                policy_version=self.policy.policy_version)
            contents=[(r.metadata,r.content) for r in reads]
        items=[]; used=0
        for meta,content in contents:
            if used+len(content)>request.max_characters:
                continue  # Whole evidence only; no invented missing facts/partial approval.
            used+=len(content)
            ref=meta.reference
            items.append(ContextItem(**ref.model_dump(exclude={"acl_revision"}),level=request.level,
                excerpt=content,parents=meta.parents or (ref,),policies=request.policies,
                epistemic_state=meta.epistemic_state))
        return ContextBundle(request_id=request.request_id,trace_id=request.trace_id,
            run_id=request.run_id,agent_id=request.agent_id,domain_id=request.domain_id,
            items=tuple(items),insufficient=not items or len(items)!=len(selected),
            loaded_characters=used,policy_version=self.policy.policy_version,
            measured_tokens=None,simulated=False,adapter="local-context-v1")
