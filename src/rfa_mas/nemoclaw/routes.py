"""Switch the gateway inference route mode without restarting sandboxes.

Preferred path: ``nemoclaw inference set --provider compatible-endpoint --model <mode> --sandbox <sb>``
(hot, documented). On a shared gateway NemoClaw refuses the change while other registered
sandboxes record a different model (``provider-model`` conflict); the documented escape hatch is
``openshell inference set``, which bypasses NemoClaw's registry checks and is therefore only
taken with ``--force-openshell`` and logged as a warning.
"""

from __future__ import annotations

import re

from rfa_mas.nemoclaw import audit
from rfa_mas.nemoclaw import bootstrap as bs
from rfa_mas.nemoclaw.config import AUTO_MODE, load_assignments, load_routing
from rfa_mas.nemoclaw.controller import Observer
from rfa_mas.nemoclaw.runner import Runner, SubprocessRunner


def current_openshell_route(runner: Runner) -> tuple[str | None, str | None]:
    result = runner.run(["openshell", "inference", "get"], timeout=60)
    provider = model = None
    for line in result.stdout.splitlines():
        m = re.match(r"\s*Provider:\s*(\S+)", line)
        if m:
            provider = m.group(1)
        m = re.match(r"\s*Model:\s*(\S+)", line)
        if m:
            model = m.group(1)
    return provider, model


def switch_route(mode: str, *, force_openshell: bool = False, runner: Runner | None = None) -> int:
    runner = runner or SubprocessRunner(cwd=bs.ROOT)
    assignments, routing = load_assignments(), load_routing()
    if mode != AUTO_MODE and mode not in routing.aliases:
        print(f"error: mode must be {AUTO_MODE} or one of {sorted(routing.aliases)}")
        return 2
    nb = assignments.host.nemoclaw_bin
    live = Observer(runner, nb).list_sandboxes()
    targets = [sb for sb in assignments.ordered_sandboxes() if sb in live]
    outcomes: list[tuple[str, bool, str]] = []
    for sandbox in targets:
        result = runner.run([nb, "inference", "set", "--provider", "compatible-endpoint", "--model", mode,
                             "--sandbox", sandbox], timeout=300)
        text = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        outcomes.append((sandbox, result.ok, text[0][:160]))
        print(f"{'ok' if result.ok else 'refused'}: nemoclaw inference set --model {mode} --sandbox {sandbox}  {text[0][:160]}")
        if not result.ok:
            break
    if outcomes and all(ok for _, ok, _ in outcomes):
        audit.record(kind="policy", verdict="ok", action="switch-route", detail={"mode": mode, "path": "nemoclaw", "sandboxes": targets})
        provider, model = current_openshell_route(runner)
        print(f"live route: provider={provider} model={model}")
        return 0
    if not force_openshell:
        print("NemoClaw refused the route change (shared gateway: peer sandboxes record a different model).\n"
              "Re-run with --force-openshell to apply `openshell inference set` directly. That path bypasses\n"
              "NemoClaw's registry checks; `nemoclaw <sb> status` will report routeDrift until re-aligned.")
        return 1
    provider, _ = current_openshell_route(runner)
    if provider is None:
        print("error: could not read the current OpenShell provider")
        return 1
    print(f"WARNING: forcing `openshell inference set --provider {provider} --model {mode}` (bypasses NemoClaw registry)")
    result = runner.run(["openshell", "inference", "set", "--provider", provider, "--model", mode], timeout=120)
    if not result.ok:
        print((result.stderr or result.stdout).strip()[-300:])
        return 1
    provider, model = current_openshell_route(runner)
    audit.record(kind="policy", verdict="forced", action="switch-route",
                 detail={"mode": mode, "path": "openshell", "provider": provider, "warning": "registry bypass"})
    print(f"live route: provider={provider} model={model} (forced; expect routeDrift in nemeclaw status)")
    return 0
