"""Lifecycle-owned SQLite checkpoints and same-host POSIX thread exclusion.

This protects local invocation coordination, not sandboxing or tool exactly-once.
The SQLite saver serializes DB calls; the separate flock spans an invocation.
"""

import asyncio
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from rfa_mas.adapters.local import _sqlite_setup_guard
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
    _setup_timeout_seconds = 5.0

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
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RfaError("configuration_error", "checkpoint 파일은 symlink일 수 없습니다.")
        # Set the DB mode before WAL/SHM creation so sidecars inherit private mode.
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        stack = AsyncExitStack()
        try:
            async with AsyncExitStack() as setup:
                deadline = asyncio.get_running_loop().time() + self._setup_timeout_seconds
                while True:
                    try:
                        setup.enter_context(_sqlite_setup_guard(self.path, blocking=False))
                    except BlockingIOError:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise RfaError(
                                "storage_busy", "checkpoint 초기화가 진행 중입니다."
                            ) from None
                        await asyncio.sleep(min(0.01, remaining))
                    else:
                        break
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
                    # Its instance lock cannot serialize a second container/process.
                    await saver.aget_tuple({"configurable": {"thread_id": "schema-initialization"}})
                    async with connection.execute("PRAGMA journal_mode") as cursor:
                        mode = await cursor.fetchone()
                    if not mode or mode[0] != "wal":
                        raise RfaError(
                            "configuration_error", "checkpoint WAL 초기화에 실패했습니다."
                        )
                    self.path.chmod(0o600)
                except BaseException:
                    # Drain/close the SQLite worker before releasing the setup lock,
                    # including cancellation while its queued SQL is still running.
                    await stack.aclose()
                    raise
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
