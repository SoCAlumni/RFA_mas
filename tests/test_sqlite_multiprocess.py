"""P0-005A: a second SQLite process must never crash the API process.

POSIX drops every fcntl lock a process holds on a file when that process closes *any*
descriptor for it. The repository's private-file checks used to open and close the DB,
-wal and -shm next to live connections, which released SQLite's -shm dead-man-switch
read lock; a second process (the separate scheduler, any reader) could then reset the
WAL index under the server's mapping (SIGBUS, "disk I/O error"). These tests use real
child processes, synthetic data, loopback only, and bounded time.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from rfa_mas.adapters.local import SqliteWorkRepository
from rfa_mas.errors import RfaError

# SQLite unix VFS: UNIX_SHM_BASE = (22 + SQLITE_SHM_NLOCK) * 4 = 120, DMS = 120 + 8.
SHM_DMS_OFFSET = 128

PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, int(sys.argv[2]))
except OSError:
    print("held")
else:
    fcntl.lockf(fd, fcntl.LOCK_UN, 1, int(sys.argv[2]))
    print("free")
finally:
    os.close(fd)
"""

READER = """
import sqlite3, sys, time
end = time.monotonic() + float(sys.argv[2])
count = 0
while time.monotonic() < end:
    connection = sqlite3.connect(sys.argv[1], timeout=10)
    connection.execute("SELECT count(*) FROM runs").fetchone()
    connection.close()
    count += 1
    time.sleep(0.005)
print(count)
"""


def _dms_lock_seen_by_another_process(shm: Path) -> str:
    """'held' when a separate process cannot take the -shm DMS byte exclusively."""
    completed = subprocess.run(
        [sys.executable, "-c", PROBE, str(shm), str(SHM_DMS_OFFSET)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    # macOS /var is a symlink; the trace dir and DB checks refuse symlinked paths.
    return tmp_path.resolve()


async def _initialized(root: Path) -> SqliteWorkRepository:
    repository = SqliteWorkRepository(root / "locks.db")
    await repository.initialize()
    return repository


async def test_private_file_checks_keep_sqlite_locks_for_other_processes(root):
    repository = await _initialized(root)
    shm = Path(str(repository.path) + "-shm")
    with repository._connect() as connection:
        connection.execute("SELECT count(*) FROM runs").fetchone()  # maps -shm, DMS lock
        assert _dms_lock_seen_by_another_process(shm) == "held"
        repository._private_files()
        with repository._connect() as nested:  # pre- and post-connect checks again
            nested.execute("SELECT 1").fetchone()
        assert _dms_lock_seen_by_another_process(shm) == "held"


async def test_private_file_checks_never_open_existing_sqlite_files(root, monkeypatch):
    repository = await _initialized(root)
    guarded = {str(repository.path) + suffix for suffix in ("", "-wal", "-shm")}
    opened: list[str] = []
    original_open = os.open

    def recording_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    with repository._connect() as connection:
        connection.execute("SELECT count(*) FROM runs").fetchone()
        repository._private_files()
    await repository.local_principal()
    await repository.initialize()  # A second initialize beside an existing DB.
    assert not guarded & set(opened)


def _touch_sidecars(database: Path) -> None:
    for sidecar in ("-wal", "-shm"):
        Path(str(database) + sidecar).touch(mode=0o600)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
async def test_sidecar_unlinked_during_lstat_is_tolerated_but_the_db_is_not(
    root, monkeypatch, suffix
):
    # SQLite unlinks -wal/-shm when its final connection closes. lstat can resolve the
    # path and then report that just-unlinked inode with st_nlink == 0 (seen under load).
    repository = await _initialized(root)
    _touch_sidecars(repository.path)
    target = str(repository.path) + suffix
    original_lstat = os.lstat

    def unlinked_during_lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if os.fspath(path) != target:
            return info
        fields = list(info[:10])
        fields[3] = 0  # st_nlink
        return os.stat_result(fields)

    monkeypatch.setattr(os, "lstat", unlinked_during_lstat)
    if suffix:
        repository._private_files()
    else:
        with pytest.raises(RfaError) as error:
            repository._private_files()
        assert error.value.code == "configuration_error"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _start_api(root: Path) -> tuple[subprocess.Popen, str, Path]:
    port = _free_loopback_port()
    values = {
        "APP_ENV": "development",
        "APP_HOST": "127.0.0.1",
        "APP_PORT": str(port),
        "DATABASE_URL": f"sqlite:///{root / 'rfa.db'}",
        "TRACE_DIR": str(root / "traces"),
        "MODEL_PROVIDER": "mock",
        "RETRIEVER_BACKEND": "local",
        "RESPONSE_BACKEND": "mock",
        "TOOL_BACKEND": "mock",
        "RUNTIME_BACKEND": "local",
        "POLICY_BACKEND": "local",
        "TRACE_BACKEND": "local",
        "SCHEDULER_ENABLED": "false",
        "ALLOW_EXTERNAL_WRITES": "false",
        "ALLOW_EXTERNAL_EGRESS": "false",
        "LOG_LEVEL": "WARNING",
    }
    env_file = root / "server.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    log_path = root / "server.log"
    with log_path.open("ab") as log:
        # Minimal environment: no user .env or shell variable reaches the child.
        process = subprocess.Popen(
            [sys.executable, "-m", "rfa_mas", "--env-file", str(env_file), "api"],
            cwd=root,
            env={"PATH": os.defpath, "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
    return process, f"http://127.0.0.1:{port}", log_path


async def _wait_ready(process: subprocess.Popen, base_url: str, limit: float = 30.0) -> None:
    deadline = time.monotonic() + limit
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"api exited before readiness: {process.returncode}")
            try:
                if (await client.get("/readyz")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise AssertionError("api did not become ready")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _start_reader(database: Path, seconds: float) -> subprocess.Popen:
    """The second process: short plain-sqlite3 read connections, like the scheduler's."""
    return subprocess.Popen(
        [sys.executable, "-c", READER, str(database), str(seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )


@pytest.mark.parametrize("requests", [40])
async def test_api_survives_a_second_sqlite_process(root, requests):
    server, base_url, log_path = _start_api(root)
    reader = None
    try:
        await _wait_ready(server, base_url)
        reader = _start_reader(root / "rfa.db", 10)
        codes: dict[str, int] = {}
        gate = asyncio.Semaphore(4)
        body = {
            "query": "SDK 출시일이 언제야?",
            "domain_id": "triv3",
            "target": {"audience": "owner"},
        }
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:

            async def one() -> None:
                async with gate:
                    try:
                        key = str((await client.post("/v1/work", json=body)).status_code)
                    except httpx.HTTPError as exc:
                        key = type(exc).__name__
                    codes[key] = codes.get(key, 0) + 1

            await asyncio.wait_for(asyncio.gather(*(one() for _ in range(requests))), 120)
        reads = int(reader.communicate(timeout=30)[0].strip() or 0)
        alive = server.poll() is None
        log = log_path.read_text(encoding="utf-8", errors="replace")
        assert reads > 0, "the second process never read the database"
        assert alive, f"api died with {server.returncode} beside a second SQLite process"
        assert codes == {"201": requests}, codes
        assert "disk I/O error" not in log and "malformed" not in log
    finally:
        if reader is not None and reader.poll() is None:
            reader.kill()
            reader.wait(timeout=10)
        _stop(server)
