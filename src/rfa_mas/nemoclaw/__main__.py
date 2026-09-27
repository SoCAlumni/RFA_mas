"""CLI: python -m rfa_mas.nemoclaw <command> — security-group controller, bootstrap, ops.

Commands (all read deploy/nemoclaw/{assignments,routing,censors}.yaml):
  validate        parse the three files and cross-check references
  render          write per-sandbox agents manifests + rendered presets
  plan            observe live sandboxes and print the reconcile plan (no changes)
  apply           reconcile (policy add/remove/exclude, agents apply, mcp add, policy explain --write)
  status          per-sandbox live view (presets, baseline excludes, agents, MCP)
  verify-baseline compare live static policy sections with deploy/nemoclaw/baseline
  explain         nemoclaw <sb> policy explain --write (agent policy context)
  seed            upload IDENTITY.md markers and skills into agent workspaces
  bootstrap       preflight → retire legacy → onboard in order → reconcile → seed (resumable)
  teardown        destroy declared sandboxes (reverse order)
  switch-route    change the gateway route mode without restarting sandboxes
  serve           run egress-proxy + broker + channel entry/audit (see serve.py)
  relocate        move an agent between security groups (promote/demote policy)
  requests        list/approve/deny blocked network requests (OCSF DENIED → preset)
  audit           print audit events
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from rfa_mas.nemoclaw import bootstrap as bs
from rfa_mas.nemoclaw import controller
from rfa_mas.nemoclaw.config import (
    DEPLOY_DIR,
    ConfigError,
    cross_check,
    load_assignments,
    load_censors,
    load_routing,
)
from rfa_mas.nemoclaw.manifests import write_manifests
from rfa_mas.nemoclaw.runner import SubprocessRunner


def _runner() -> SubprocessRunner:
    return SubprocessRunner(cwd=bs.ROOT)


def cmd_validate(args) -> int:
    a, r, c = load_assignments(), load_routing(), load_censors()
    problems = cross_check(a, r, c)
    print(json.dumps({"assignments": {"sandboxes": a.ordered_sandboxes(), "placement": a.placement()},
                      "routing": {"aliases": sorted(r.aliases), "channels": sorted(r.channels),
                                  "default_mode": r.proxy.default_mode},
                      "censors": {"profiles": sorted(c.profiles)},
                      "problems": problems}, ensure_ascii=False, indent=2))
    return 1 if problems else 0


def cmd_render(args) -> int:
    a = load_assignments()
    manifests = write_manifests(a, DEPLOY_DIR / "agents")
    rendered = {}
    for sb in a.sandboxes:
        for name in a.sandbox_presets(sb, fallback=True):
            rendered[name] = controller.render_preset(name, a.host.lan_ip, bs.SG_DIR / "rendered")
    for sb, path in manifests.items():
        print(f"manifest {sb}: {path.relative_to(bs.ROOT)}")
    for name, path in rendered.items():
        print(f"preset {name}: {path.relative_to(bs.ROOT)}")
    return 0


def _plan(args):
    a, r = load_assignments(), load_routing()
    runner = _runner()
    manifests = write_manifests(a, DEPLOY_DIR / "agents")
    rendered = {name: controller.render_preset(name, a.host.lan_ip, bs.SG_DIR / "rendered")
                for sb in a.sandboxes for name in a.sandbox_presets(sb, fallback=True)}
    observed = controller.Observer(runner, a.host.nemoclaw_bin).observe(a.sandboxes)
    mcp_url = None if args.skip_mcp else (
        f"https://{a.host.lan_ip}:{r.broker.bind.rsplit(':', 1)[1]}{r.broker.path}")
    inputs = controller.PlanInputs(a, observed, rendered, manifests, nemoclaw_bin=a.host.nemoclaw_bin,
                                   mcp_url=mcp_url, mcp_credential_env=r.broker.credential_env,
                                   mcp_fallback=args.mcp_fallback)
    actions = controller.plan(inputs)
    if args.sandbox:
        actions = [x for x in actions if x.sandbox in args.sandbox]
    return a, r, runner, actions


def cmd_plan(args) -> int:
    _, _, _, actions = _plan(args)
    if not actions:
        print("plan: no changes (live state matches assignments.yaml)")
    for action in actions:
        print(action.display())
    return 0


def cmd_apply(args) -> int:
    a, r, runner, actions = _plan(args)
    runnable = [x for x in actions if x.kind != "onboard"]
    skipped = [x for x in actions if x.kind == "onboard"]
    for action in skipped:
        print(f"skip (use bootstrap): {action.display()}")
    if not runnable:
        print("apply: nothing to do")
        return 0
    secrets = bs.ensure_host_secrets(r)
    from rfa_mas.nemoclaw import audit

    def record(action, result):
        status = "ok" if result.ok else "failed"
        print(f"{status}: {action.display()} ({result.duration_seconds}s)")
        audit.record(kind="policy", sandbox=action.sandbox, agent=None, channel=None, profile=None,
                     verdict=status, action=action.kind,
                     detail={"reason": action.reason, "argv": action.argv[1:6]})

    results = controller.apply(runnable, runner, secrets.get, on_result=record)
    failed = [r for _, r in results if not r.ok]
    if failed:
        print((failed[0].stderr or failed[0].stdout).strip()[-600:], file=sys.stderr)
        return 1
    return 0


def cmd_status(args) -> int:
    print(bs.json_dumps(bs.status(_runner())))
    return 0


def cmd_verify_baseline(args) -> int:
    a = load_assignments()
    baseline = yaml.safe_load((DEPLOY_DIR / "baseline" / "openclaw-sandbox.yaml").read_text())
    observer = controller.Observer(_runner(), a.host.nemoclaw_bin)
    rc = 0
    for sb in args.sandbox or a.ordered_sandboxes():
        problems = controller.verify_baseline(observer.policy_get(sb), baseline)
        print(f"{sb}: {'ok' if not problems else 'DRIFT'}")
        for p in problems:
            print(f"  {p}")
            rc = 1
    return rc


def cmd_explain(args) -> int:
    a = load_assignments()
    runner = _runner()
    for sb in args.sandbox or a.ordered_sandboxes():
        result = runner.run([a.host.nemoclaw_bin, sb, "policy", "explain", "--write"], timeout=180)
        print(f"{sb}: {'written' if result.ok else 'failed'} (POLICY.md)")
        if args.show:
            print(result.stdout)
    return 0


def cmd_seed(args) -> int:
    a, r = load_assignments(), load_routing()
    from rfa_mas.nemoclaw.markers import load_or_create_secret

    secret = load_or_create_secret(bs.ROOT / r.proxy.marker_key_file)
    runner = _runner()
    for sb in args.sandbox or a.ordered_sandboxes():
        uploaded = bs.seed_sandbox(a, sb, secret, runner, a.host.nemoclaw_bin)
        print(f"{sb}: seeded {uploaded}")
    return 0


def cmd_bootstrap(args) -> int:
    options = bs.BootstrapOptions(
        retire=tuple(args.retire) if args.retire is not None else bs.LEGACY_SANDBOXES,
        only=tuple(args.sandbox or ()), skip_mcp=args.skip_mcp, mcp_fallback=args.mcp_fallback,
        dry_run=args.dry_run,
    )
    report = bs.bootstrap(options, _runner())
    print(bs.json_dumps(report))
    return 0


def cmd_teardown(args) -> int:
    destroyed = bs.teardown(_runner(), names=tuple(args.sandbox or ()))
    print(bs.json_dumps({"destroyed": destroyed}))
    return 0


def cmd_switch_route(args) -> int:
    from rfa_mas.nemoclaw.routes import switch_route

    return switch_route(args.mode, force_openshell=args.force_openshell, runner=_runner())


def cmd_serve(args) -> int:
    from rfa_mas.nemoclaw.serve import serve

    return serve(replay=args.replay)


def cmd_relocate(args) -> int:
    from rfa_mas.nemoclaw.relocate import relocate

    return relocate(args.agent, args.to_groups.split(","), wipe=args.wipe, dry_run=args.dry_run,
                    runner=_runner())


def cmd_requests(args) -> int:
    from rfa_mas.nemoclaw.requests import main as requests_main

    return requests_main(args)


def cmd_kb_seed(args) -> int:
    from rfa_mas.nemoclaw.kb import main as kb_main

    return kb_main()


def cmd_audit(args) -> int:
    from rfa_mas.nemoclaw import audit

    for event in audit.query(limit=args.limit, kind=args.kind):
        print(json.dumps(event, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rfa_mas.nemoclaw", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate").set_defaults(func=cmd_validate)
    sub.add_parser("render").set_defaults(func=cmd_render)
    for name, func in (("plan", cmd_plan), ("apply", cmd_apply)):
        p = sub.add_parser(name)
        p.add_argument("--sandbox", nargs="*")
        p.add_argument("--skip-mcp", action="store_true")
        p.add_argument("--mcp-fallback", action="store_true", help="use REST preset instead of managed MCP")
        p.set_defaults(func=func)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("verify-baseline"); p.add_argument("sandbox", nargs="*"); p.set_defaults(func=cmd_verify_baseline)
    p = sub.add_parser("explain"); p.add_argument("sandbox", nargs="*"); p.add_argument("--show", action="store_true"); p.set_defaults(func=cmd_explain)
    p = sub.add_parser("seed"); p.add_argument("sandbox", nargs="*"); p.set_defaults(func=cmd_seed)
    p = sub.add_parser("bootstrap")
    p.add_argument("--retire", nargs="*", help="legacy sandboxes to snapshot+destroy (default: rfa-demo)")
    p.add_argument("--sandbox", nargs="*"); p.add_argument("--skip-mcp", action="store_true")
    p.add_argument("--mcp-fallback", action="store_true"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_bootstrap)
    p = sub.add_parser("teardown"); p.add_argument("sandbox", nargs="*"); p.set_defaults(func=cmd_teardown)
    p = sub.add_parser("switch-route"); p.add_argument("mode"); p.add_argument("--force-openshell", action="store_true")
    p.set_defaults(func=cmd_switch_route)
    p = sub.add_parser("serve"); p.add_argument("--replay", action="store_true", help="no upstream calls; canned answers")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("relocate"); p.add_argument("agent"); p.add_argument("--to-groups", required=True)
    p.add_argument("--wipe", action="store_true"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_relocate)
    p = sub.add_parser("requests"); p.add_argument("action", choices=["list", "approve", "deny", "sync"])
    p.add_argument("request_id", nargs="?"); p.add_argument("--sandbox"); p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_requests)
    sub.add_parser("kb-seed", help="seed synthetic public notes into the local KB the intranet API serves").set_defaults(func=cmd_kb_seed)
    p = sub.add_parser("audit"); p.add_argument("--limit", type=int, default=50); p.add_argument("--kind")
    p.set_defaults(func=cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
