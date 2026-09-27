"""Read-only same-service PoC catalog; replace with a task-list API adapter later.

Only discovers IDs from this core's existing owner registry. The repository's
public lifecycle method rechecks ownership and validates definitions. No writes,
private repository calls, teammate DB access, or extra ownership authority.
"""

import asyncio
import sqlite3

from rfa_mas.errors import RfaError


class SqliteTeamCatalog:
    def __init__(self, repository):
        self.repository = repository

    async def list_for(self, principal):
        if not principal.authenticated or not principal.user_id:
            raise RfaError("authentication_required", "유효한 인증이 필요합니다.")

        def ids():
            uri = self.repository.path.resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True)
            try:
                return [
                    row[0]
                    for row in db.execute(
                        "SELECT o.task_id FROM product_task_owners o "
                        "JOIN team_slots s ON s.task_id=o.task_id "
                        "WHERE o.owner_id=? ORDER BY o.task_id",
                        (principal.user_id,),
                    )
                ]
            finally:
                db.close()

        records = []
        for task_id in await asyncio.to_thread(ids):
            records.append(await self.repository.get_team_lifecycle(task_id, principal))
        return records
