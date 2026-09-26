"""Real local lexical search and deliberately bounded, local-only context reader."""
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from rfa_mas.application.source_access import BoundAccess
from rfa_mas.contracts import (
    ContextBundle, ContextItem, ContextLevel, ContextRequest, EvidenceBundle, EvidenceItem,
    PolicyBindings, PolicyDecisionV11, PolicyRequest, RetrievalRequest, new_id,
)
from rfa_mas.errors import RfaError


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
