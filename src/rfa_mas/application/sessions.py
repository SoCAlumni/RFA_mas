"""Authorized metadata/history operations, not a LangGraph checkpoint engine."""

from rfa_mas.contracts import RunRecord, SessionDetail, SessionRecord, TrustedPrincipal
from rfa_mas.ports import WorkRepositoryPort


class SessionService:
    def __init__(self, repository: WorkRepositoryPort) -> None:
        self._repository = repository

    async def local_principal(self) -> TrustedPrincipal:
        return await self._repository.local_principal()

    async def create(self, principal: TrustedPrincipal) -> SessionRecord:
        return await self._repository.create_session(principal)

    async def list(self, principal: TrustedPrincipal) -> list[SessionRecord]:
        return await self._repository.list_sessions(principal)

    async def get(self, session_id: str, principal: TrustedPrincipal) -> SessionDetail:
        return await self._repository.get_session(session_id, principal)

    async def get_run(self, run_id: str, principal: TrustedPrincipal) -> RunRecord:
        return await self._repository.get_owned_run(run_id, principal)
