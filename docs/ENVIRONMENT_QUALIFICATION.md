# Isolated CPython / SQLite qualification — 2026-09-26 UTC

This is an authorized environment diagnostic, not task completion, product E2E,
live NVIDIA validation, or a reproduction of SQLite's WAL-reset defect.
No shared interpreter, existing virtual environment, source, task, or canonical
evidence was modified. No credentials or dotenv files were read.

## Provenance

- Host: macOS arm64.
- Existing uv: 0.11.3 (Homebrew 2026-04-01).
- Existing root interpreter: managed CPython 3.12.13, March 25 build, SQLite 3.50.4.
- Qualified interpreter: `pythons/cpython-3.12.13-macos-aarch64-none/bin/python3.12`.
- Qualified isolated project environment: `venv/bin/python`.
- Measured qualified Python: 3.12.13, build `main`, `Aug 7 2026 02:15:23`.
- Measured qualified SQLite: 3.53.1.
- SQLite source ID: `2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9`.
- Official PBS release: https://github.com/astral-sh/python-build-standalone/releases/tag/20260807
- Artifact: https://github.com/astral-sh/python-build-standalone/releases/download/20260807/cpython-3.12.13%2B20260807-aarch64-apple-darwin-install_only_stripped.tar.gz
- Downloaded artifact size: 25,008,370 bytes.
- Expected and independently streamed SHA256: `25baa97c65b3f0aa90e21131b4f9e80aef8899e8144006db8a9d2c1ab9e807e3`.
- Pinned official uv metadata: https://raw.githubusercontent.com/astral-sh/uv/299a93de4b94e754f260c673d2de456afbdd4fb7/crates/uv-python/download-metadata.json
- Same-release SQLite build input: https://raw.githubusercontent.com/astral-sh/python-build-standalone/20260807/pythonbuild/downloads.py (`actual_version=3.53.1.0`).

SQLite's fix notice: https://sqlite.org/wal.html#walresetbug . This runtime is above
the fixed 3.51.3 threshold. This does not remove ordinary SQLITE_BUSY behavior or
replace the separately ongoing application startup-race fix.

## Executed commands and results

The parent explicitly authorized an isolated install, small concurrency check,
and locked NAT-enabled temporary environment. Before installation, `uv python
install --help` and `uv sync --help` were inspected. The metadata override was
confirmed using `uv python list --no-config --only-downloads --show-urls
--python-downloads-json-url <pinned metadata above> 3.12.13`.

Temporary root creation: `mktemp -d /tmp/rfa-sqlite-qualification.XXXXXX` returned
`/tmp/rfa-sqlite-qualification.IKbCas`, canonical `/private/tmp/...` used below.

```sh
env -i PATH=/opt/homebrew/bin:/usr/bin:/bin uv python install \
  --no-config --no-bin --no-progress \
  --install-dir /private/tmp/rfa-sqlite-qualification.IKbCas/pythons \
  --cache-dir /private/tmp/rfa-sqlite-qualification.IKbCas/cache \
  --python-downloads-json-url https://raw.githubusercontent.com/astral-sh/uv/299a93de4b94e754f260c673d2de456afbdd4fb7/crates/uv-python/download-metadata.json \
  3.12.13
```

Exit 0: installed Python 3.12.13 in 5.38s. No `--reinstall`, `--force`, shared
installation directory, or executable registration was used.

The official artifact was separately streamed with `urllib.request.urlopen`,
1 MiB chunks into `hashlib.sha256`, using the existing interpreter without
loading settings. Its digest exactly matched the pinned metadata/release asset.
No archive was executed by that hash check.

```sh
env -i PATH=/usr/bin:/bin \
  /private/tmp/rfa-sqlite-qualification.IKbCas/pythons/cpython-3.12.13-macos-aarch64-none/bin/python3.12 \
  -B /private/tmp/rfa-sqlite-qualification.IKbCas/qualify_sqlite.py
```

Exit 0, 08:47:01.506283–08:47:01.713599 UTC:

- Three spawned processes, 50 separate `BEGIN IMMEDIATE`/insert/commit cycles each.
- Each process also performed periodic passive checkpoints.
- Exactly 150 rows and the expected sequence sum remained.
- TRUNCATE checkpoint returned `(0, 0, 0)`.
- `integrity_check` returned `ok` before and after reopening; WAL persisted.
- Script and synthetic DB remain in this temporary root. The script requires a
  new DB and intentionally refuses to overwrite its previous run.

```sh
env -i PATH=/opt/homebrew/bin:/usr/bin:/bin \
  UV_PROJECT_ENVIRONMENT=/private/tmp/rfa-sqlite-qualification.IKbCas/venv \
  uv sync --locked --extra nat --no-config --no-progress --no-python-downloads \
  --python /private/tmp/rfa-sqlite-qualification.IKbCas/pythons/cpython-3.12.13-macos-aarch64-none/bin/python3.12 \
  --cache-dir /private/tmp/rfa-sqlite-qualification.IKbCas/cache \
  --project /Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas
```

Exit 0: resolved 169 packages in 10ms, prepared 168 in 35.42s, installed 168 in
603ms. Used the existing lock without package updates.

```sh
env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /private/tmp/rfa-sqlite-qualification.IKbCas/venv/bin/python -B -m pytest -q \
  -o cache_dir=/private/tmp/rfa-sqlite-qualification.IKbCas/pytest-cache \
  --basetemp=/private/tmp/rfa-sqlite-qualification.IKbCas/pytest-tmp \
  /Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas/tests/spikes/test_nat_compatibility.py
```

cwd: canonical source root. Exit 0: **8 passed in 9.24s**, no skipped tests. The
existing spike uses actual NAT 1.8.0 wrapper registration with fake model/tool
state and prohibits network sockets. It is not the P0-028 product NAT adapter.

A separate `env -i PATH=/usr/bin:/bin <isolated venv>/bin/python -B -u -` stdin
probe used actual `AsyncSqliteSaver.from_conn_string`, a one-node StateGraph,
`durability='sync'`, and a new `langgraph-synthetic.sqlite` in this directory:

1. Input `value=1` saved result `value=2` with a synthetic thread ID.
2. Closed the first saver and opened a second saver on the same temporary file.
3. `aget_state` returned `value=2`; the next invocation returned `value=3`.
4. An independent aiosqlite connection returned `integrity_check=ok`.

Exit 0. Measured packages: LangGraph 1.2.12, checkpoint 4.2.0, SQLite saver 3.1.1,
aiosqlite 0.22.1, nvidia-nat-langchain 1.8.0. aiosqlite uses stdlib sqlite3; no
driver replacement or global monkeypatch was used.

## Preservation and limits

Before/after source hashes were identical:

| File | SHA256 |
| --- | --- |
| pyproject.toml | 767555e89c26cd14d502864a4b8e7409534a8a21b1660fd13eaaacb1b4afd325 |
| uv.lock | cbc662503129c4b6e550dbea9f7d932a7107494835507e911ab972edcf932b83 |
| .python-version | 9ea280e4c89d3f302c1e8b3e5e7db91c46bec0fe761356ae90f32aaf0dbe0e8b |

Final root interpreter check still returned the original shared installation and
SQLite 3.50.4. Canonical operational task files were changing under the
coordinator; this diagnostic did not edit them. Qualification commands all
completed on their first execution; no failed install/test was retried.

The temporary tree occupies about 1.4 GiB including its isolated download cache.
It has deliberately been retained for coordinator inspection, not selected as
the project's permanent runtime. OS temporary-directory cleanup can remove it.
Before any coordinated permanent switch: stop affected running sessions, choose
a durable isolated Python location, recreate qualified environments from the
unchanged lock, check actual SQLite again, and run the required product regression.
Do not overwrite the shared Python while other agents are executing tests.

## Durable preparation and coordinated selection

The coordinator subsequently approved a separate ignored install under
`.local/toolchains/pbs20260807/pythons`, using the same pinned metadata and cache.
Actual install command changed only `--install-dir` to this absolute repository
path. One install completed successfully in 17.20s. A premature version query
before installer completion returned a missing executable; it was not counted as
success or retried as an installation. After completion, explicit assertions
confirmed CPython3.12.13, arm64 and SQLite3.53.1 with the source ID above.

The interpreter is now available at:

```text
/Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas/.local/toolchains/pbs20260807/pythons/cpython-3.12.13-macos-aarch64-none/bin/python3.12
```

After all canonical test processes finished, the coordinator retained the old
root `.venv` at `.local/retained-envs/root-before-pbs20260807` and invoked:

```sh
env -i PATH=/opt/homebrew/bin:/usr/bin:/bin \
  uv sync --locked --extra nat --no-config --no-progress --no-python-downloads \
  --python /Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas/.local/toolchains/pbs20260807/pythons/cpython-3.12.13-macos-aarch64-none/bin/python3.12 \
  --cache-dir /private/tmp/rfa-sqlite-qualification.IKbCas/cache \
  --project /Users/minseop/Dev/projects/nvidia_hackathon_2026/rfa_mas
```

This deliberately recreates only the idle canonical virtual environment from
the unchanged lock. Existing worktree environments and shared uv Python are not
replaced. The retained environment is recoverable by restoring its original
location; its executable entrypoints retain their original absolute shebangs.
New worker environments should use the explicit durable `--python` path above.
The coordinator records measured post-switch regression separately in WORK_LOG;
selection of a patched version is not by itself a product acceptance result.
