"""Owner-authenticated note/export storage. No connector, search engine or egress."""

from rfa_mas.contracts import (
    DomainId,
    KnowledgeDelete,
    KnowledgeExport,
    KnowledgeImportResult,
    KnowledgeImportRow,
    KnowledgeRevision,
    KnowledgeWrite,
    TrustedPrincipal,
)
from rfa_mas.errors import RfaError
from rfa_mas.ports import PolicyPort, WorkRepositoryPort


class KnowledgeService:
    def __init__(self, repository: WorkRepositoryPort, policy: PolicyPort):
        self.repository, self.policy = repository, policy

    async def write(
        self,
        request: KnowledgeWrite,
        principal: TrustedPrincipal,
        *,
        source_id: str | None = None,
    ) -> KnowledgeRevision:
        return await self.repository.write_knowledge(
            request,
            principal,
            policy_version=self.policy.policy_version,
            source_id=source_id,
        )

    async def get(
        self, source_id: str, principal: TrustedPrincipal, *, revision: str | None = None
    ):
        return await self.repository.get_knowledge(source_id, principal, revision=revision)

    async def list(self, principal: TrustedPrincipal, *, domain_id: DomainId | None = None):
        return await self.repository.list_knowledge(principal, domain_id=domain_id)

    async def delete(self, source_id: str, request: KnowledgeDelete, principal: TrustedPrincipal):
        return await self.repository.delete_knowledge(source_id, request, principal)

    async def import_export(self, batch: KnowledgeExport, principal: TrustedPrincipal):
        # A transport-authenticated principal is still rechecked by the repository
        # on every independent row. A row never supplies identity or membership grants.
        batch = KnowledgeExport.model_validate_json(batch.model_dump_json(), strict=True)
        try:
            principal = TrustedPrincipal.model_validate(principal.model_dump(), strict=True)
            if principal.authenticated is not True or not principal.user_id:
                raise ValueError("authentication required")
        except (ValueError, AttributeError):
            raise RfaError("authentication_required", "자료 접근에 인증이 필요합니다.") from None
        receipts = []
        for index, row in enumerate(batch.rows):
            try:
                id_field, revision_field = (
                    ("number", "revision")
                    if batch.provider == "github_issue"
                    else ("id", "version")
                )
                allowed = {
                    id_field,
                    revision_field,
                    "title",
                    "body",
                    "updated_at",
                    "acl",
                    "expected_revision",
                }
                if set(row) - allowed:
                    raise ValueError("unknown export field")
                external_id = row[id_field]
                if type(external_id) is int:
                    external_id = str(external_id)
                if not isinstance(external_id, str) or not external_id:
                    raise ValueError("invalid source ID")
                request = KnowledgeWrite.model_validate(
                    {
                        "domain_id": batch.domain_id,
                        "provenance": {
                            "provider": batch.provider,
                            "namespace": batch.namespace,
                            "external_id": external_id,
                        },
                        "provider_revision": row[revision_field],
                        "title": row["title"],
                        "content": row["body"],
                        "acl": row.get("acl", {}),
                        "source_modified_at": row.get("updated_at"),
                        "expected_revision": row.get("expected_revision"),
                        "synthetic": batch.synthetic,
                    }
                )
                record = await self.write(request, principal)
                receipts.append(
                    KnowledgeImportRow(
                        row=index,
                        status="accepted",
                        source_id=record.document.source_id,
                        source_revision=record.document.source_revision,
                    )
                )
            except (ValueError, KeyError, TypeError):
                receipts.append(
                    KnowledgeImportRow(row=index, status="rejected", error_code="invalid_input")
                )
            except RfaError as exc:
                if exc.code not in {"policy_denied", "idempotency_conflict", "not_found"}:
                    # Storage/authentication failures are not successful partial imports.
                    raise
                receipts.append(
                    KnowledgeImportRow(row=index, status="rejected", error_code=exc.code)
                )
        return KnowledgeImportResult(rows=tuple(receipts))
