"""Idempotent demo seeding for a running local PoC through its own same-origin UI API.

Loads synthetic KB notes, creates Task teams through the product chat path (an explicit
research/benchmark request that the core Supervisor turns into one durable Task + team),
and links each Task to its related KB. Nothing writes the runtime DB directly and nothing
leaves loopback. Re-running is safe: notes reuse their fixture key as the idempotency key,
a Task whose goal text already exists is reused, and links are upserts.

    python -m rfa_mas.poc.seed --url http://127.0.0.1:8780 --fixture fixtures/poc/demo_seed.json
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "poc" / "demo_seed.json"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def load_fixture(path: Path) -> dict:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("synthetic") is not True or not isinstance(fixture.get("notes"), list):
        raise ValueError("fixture must declare synthetic=true and a notes list")
    keys = [n["key"] for n in fixture["notes"]] + [t["key"] for t in fixture.get("tasks", [])]
    if len(set(keys)) != len(keys):
        raise ValueError("fixture keys must be unique")
    return fixture


async def _check(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path}: "
            f"{response.status_code} {response.text[:300]}"
        )
    return response.json()


async def seed(client: httpx.AsyncClient, fixture: dict) -> dict:
    """Apply the fixture through the UI API of the given client; returns a safe report."""
    csrf = (await _check(await client.get("/ui/api/csrf")))["csrf_token"]
    client.headers["X-RFA-CSRF"] = csrf
    report = {
        "dataset": fixture.get("dataset"),
        "started_at": datetime.now(UTC).isoformat(),
        "notes_before": len(await _check(await client.get("/ui/api/notes"))),
        "notes": [],
        "tasks": [],
    }
    sources: dict[str, str] = {}
    for note in fixture["notes"]:
        created = await _check(
            await client.post(
                "/ui/api/notes",
                json={k: note[k] for k in ("domain_id", "title", "content")},
                headers={"Idempotency-Key": note["key"]},
            )
        )
        document = created["document"]
        sources[note["key"]] = document["source_id"]
        report["notes"].append(
            {
                "key": note["key"],
                "source_id": document["source_id"],
                "source_revision": document["source_revision"],
                "audience": document["audience"],
            }
        )
    for task in fixture.get("tasks", []):
        entry = {"key": task["key"], "goal": task["text"], "created": False}
        assignees = await _check(await client.get("/ui/api/chat/assignees"))
        existing = next((t for t in assignees if t["goal"] == task["text"][:160]), None)
        if existing is None:
            session = await _check(await client.post("/ui/api/sessions", json={}))
            result = await _check(
                await client.post(
                    f"/ui/api/sessions/{session['session_id']}/chat",
                    json={
                        "text": task["text"],
                        "message_id": task["key"],
                        "domain_id": task["domain_id"],
                    },
                )
            )
            route = result["route"]
            if route.get("kind") != "new_task" or not route.get("task_id"):
                raise RuntimeError(
                    f"{task['key']}: expected a new Task team, got route {route.get('kind')}"
                    f"/{route.get('reason')}"
                )
            entry.update(
                created=True,
                session_id=session["session_id"],
                run_id=result["run_id"],
                team_status=(result.get("team") or {}).get("status"),
                simulated=(result.get("team") or {}).get("simulated"),
            )
            task_id = route["task_id"]
        else:
            task_id = existing["task_id"]
        entry["task_id"] = task_id
        wanted = [sources[k] for k in task.get("linked_note_keys", [])]
        if task.get("link_subject_matches"):
            view = await _check(await client.get(f"/ui/api/teams/{task_id}"))
            wanted.extend(m["source_id"] for m in view["subject_matches"])
        if wanted:
            view = await _check(
                await client.post(
                    f"/ui/api/teams/{task_id}/kb",
                    json={"source_ids": list(dict.fromkeys(wanted)), "note": task["key"]},
                )
            )
        else:
            view = await _check(await client.get(f"/ui/api/teams/{task_id}"))
        entry.update(
            team_id=view["team"]["team_id"],
            pattern=view["team"]["pattern"],
            team_state=view["team"]["state"],
            members=[m["role"] for m in view["team"]["members"]],
            linked_sources=len(view["linked_sources"]),
        )
        report["tasks"].append(entry)
    report["notes_after"] = len(await _check(await client.get("/ui/api/notes")))
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


async def run(url: str, fixture_path: Path) -> dict:
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError("seed only targets a loopback PoC URL")
    fixture = load_fixture(fixture_path)
    async with httpx.AsyncClient(
        base_url=url, headers={"Origin": url.rstrip("/")}, trust_env=False, timeout=180
    ) as client:
        return await seed(client, fixture)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8780")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)
    report = asyncio.run(run(args.url, args.fixture))
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
