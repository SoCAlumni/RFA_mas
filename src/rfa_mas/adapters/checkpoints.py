"""Lifecycle-owned SQLite checkpoints and same-host POSIX thread exclusion.

This protects local invocation coordination, not sandboxing or tool exactly-once.
The SQLite saver serializes DB calls; the separate flock spans an invocation.
"""

from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
    EvidenceRef,
    PublicationStatus,
    ReviewDecision,
    ReviewStatus,
    SimulationScenario,
    SourceLocation,
    StructuredError,
    WorkRequest,
    WorkStatus,
    sha256_text,
)
from rfa_mas.errors import RfaError


class SqliteCheckpoints:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._stack: AsyncExitStack | None = None
        self.saver: AsyncSqliteSaver | None = None

    async def start(self) -> AsyncSqliteSaver:
        if self.saver is not None:
            return self.saver
        try:
            import fcntl  # noqa: F401 -- fail explicitly on unsupported hosts
        except ImportError as exc:
            raise RfaError(
                "configuration_error", "동일-host POSIX thread lock이 필요합니다."
            ) from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stack = AsyncExitStack()
        try:
            connection = await stack.enter_async_context(aiosqlite.connect(str(self.path)))
            serde = JsonPlusSerializer(
                pickle_fallback=False,
                allowed_msgpack_modules=[
                    WorkRequest,
                    DraftBundle,
                    DraftTarget,
                    EvidenceRef,
                    SourceLocation,
                    ReviewDecision,
                    StructuredError,
                    DomainId,
                    Audience,
                    WorkStatus,
                    PublicationStatus,
                    ReviewStatus,
                    SimulationScenario,
                ],
            )
            saver = AsyncSqliteSaver(connection, serde=serde)
            # Official saver lazily initializes its own tables on first use.
            await saver.aget_tuple({"configurable": {"thread_id": "schema-initialization"}})
            self.path.chmod(0o600)
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self.saver = stack, saver
        return saver

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack, self.saver = None, None

    @contextmanager
    def guard(self, thread_id: str) -> Any:
        import fcntl

        directory = self.path.parent / (self.path.name + ".locks")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Server ID is hashed only for a safe filename, never as anonymization.
        with (directory / (sha256_text(thread_id) + ".lock")).open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RfaError("thread_busy", "같은 세션의 실행이 진행 중입니다.") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
