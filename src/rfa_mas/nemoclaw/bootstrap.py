"""Host-side bootstrap: keys, TLS, preflight, onboarding (resumable), seeding, MCP registration.

Every step checks the live state first so ``make bootstrap`` can be re-run after a failure
and skips what already exists. Credentials only travel through process environment to the
NemoClaw CLI; nothing prints them.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from rfa_mas.nemoclaw import controller
from rfa_mas.nemoclaw.config import (
    DEPLOY_DIR,
    Assignments,
    ConfigError,
    Routing,
    load_assignments,
    load_censors,
    load_routing,
)
from rfa_mas.nemoclaw.manifests import (
    openclaw_agent_id,
    render_head_identity,
    render_identity,
    workspace_path,
    write_manifests,
)
from rfa_mas.nemoclaw.markers import load_or_create_secret
from rfa_mas.nemoclaw.runner import CommandError, Runner, SubprocessRunner, extract_json

ROOT = DEPLOY_DIR.parents[1]
SG_DIR = ROOT / ".local" / "sg"
LEGACY_SANDBOXES = ("rfa-demo",)


def log(message: str) -> None:
    print(f"[sg] {message}", flush=True)


# --------------------------------------------------------------------------- secrets / files


def load_or_create_key(path: Path) -> str:
    path = path.expanduser()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 32:
            raise ConfigError(f"{path}: key too short")
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(36)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(value + "\n", encoding="utf-8")
    return value


@dataclass
class HostSecrets:
    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        return self.values.get(key)


def ensure_host_secrets(routing: Routing, root: Path = ROOT) -> HostSecrets:
    proxy_key = load_or_create_key(root / routing.proxy.key_file)
    broker_key = load_or_create_key(root / routing.broker.key_file)
    load_or_create_secret(root / routing.proxy.marker_key_file)
    return HostSecrets(
        {routing.proxy.credential_env: proxy_key, routing.broker.credential_env: broker_key}
    )


def check_env_file(path: Path, required: str, root: Path = ROOT) -> None:
    """The proxy's upstream key file must exist, be owner-only and git-ignored; fail otherwise."""
    if not path.exists():
        raise ConfigError(f"{path}: missing (create it with `rfa init-env` and add {required})")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"{path}: mode {oct(mode)} is too open; chmod 600")
    probe = subprocess.run(
        ["git", "check-ignore", "-q", str(path)], cwd=str(root), capture_output=True, check=False
    )
    if probe.returncode != 0:
        raise ConfigError(f"{path}: must be git-ignored")
    found = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{required}=") and line.split("=", 1)[1].strip():
            found = True
    if not found:
        raise ConfigError(f"{path}: {required} is empty (value never printed)")


def read_env_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise ConfigError(f"{path}: {key} not set")


# --------------------------------------------------------------------------- TLS (managed MCP)


def ensure_tls(tls_dir: Path, lan_ip: str, runner: Runner) -> Path:
    """Local CA + server certificate with an IP SAN for the broker MCP endpoint.

    NemoClaw's managed MCP accepts only HTTPS and, for a private IP literal, requires a
    certificate with a matching IP subject alternative name; the CA goes to onboarding via
    ``NEMOCLAW_CORPORATE_CA_BUNDLE`` (must contain CA:TRUE certificates only).
    """
    tls_dir.mkdir(parents=True, exist_ok=True)
    ca_key, ca_pem = tls_dir / "ca.key", tls_dir / "ca.pem"
    srv_key, srv_pem, srv_csr = tls_dir / "server.key", tls_dir / "server.pem", tls_dir / "server.csr"
    if ca_pem.exists() and srv_pem.exists() and srv_key.exists():
        return ca_pem
    for path in (ca_key, srv_key):
        path.touch(mode=0o600)
        path.chmod(0o600)
    runner.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key),
         "-out", str(ca_pem), "-days", "30", "-subj", "/CN=rfa-sg-local-ca",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
        timeout=60, check=True,
    )
    ext = tls_dir / "server.ext"
    ext.write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        f"subjectAltName=IP:{lan_ip},DNS:localhost,IP:127.0.0.1\n",
        encoding="utf-8",
    )
    runner.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(srv_key),
                "-out", str(srv_csr), "-subj", f"/CN={lan_ip}"], timeout=60, check=True)
    runner.run(["openssl", "x509", "-req", "-in", str(srv_csr), "-CA", str(ca_pem), "-CAkey", str(ca_key),
                "-CAcreateserial", "-out", str(srv_pem), "-days", "30", "-extfile", str(ext)],
               timeout=60, check=True)
    log(f"tls: issued broker certificate for IP {lan_ip} under local CA {ca_pem}")
    return ca_pem


# --------------------------------------------------------------------------- preflight


def check_ollama(model: str, runner: Runner) -> None:
    result = runner.run(["ollama", "list"], timeout=30)
    if not result.ok:
        raise ConfigError("ollama is not running (start it, then re-run)")
    if model.split(":")[0] not in result.stdout:
        raise ConfigError(f"ollama model {model} not pulled (`ollama pull {model}`)")


def check_health(url: str, name: str, attempts: int = 30, pause: float = 1.0) -> dict:
    last = ""
    for _ in range(attempts):
        try:
            response = httpx.get(url, timeout=3.0)
            if response.status_code == 200:
                return response.json()
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(pause)
    raise ConfigError(f"{name} not healthy at {url}: {last}")


def check_nemoclaw(runner: Runner, nemoclaw_bin: str) -> str:
    result = runner.run([nemoclaw_bin, "--version"], timeout=60)
    if not result.ok:
        raise ConfigError("nemoclaw CLI not runnable (expected ~/.hermes/node/bin/nemoclaw)")
    return result.stdout.strip().splitlines()[-1]


# --------------------------------------------------------------------------- onboarding


def onboard_env(routing: Routing, host_secrets: HostSecrets, ca_bundle: Path | None) -> dict[str, str]:
    env = {
        "NEMOCLAW_PROVIDER": "custom",
        "NEMOCLAW_ENDPOINT_URL": routing.proxy.route_url,
        "NEMOCLAW_MODEL": routing.proxy.default_mode,
        "NEMOCLAW_PREFERRED_API": "openai-completions",
        "NEMOCLAW_POLICY_TIER": "restricted",
        "NEMOCLAW_YES": "1",
        "NEMOCLAW_NON_INTERACTIVE": "1",
        "NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE": "1",
        "NEMOCLAW_ONBOARD_VALIDATION_TIMEOUT_SECONDS": "240",
        routing.proxy.credential_env: host_secrets.values[routing.proxy.credential_env],
    }
    if ca_bundle is not None:
        env["NEMOCLAW_CORPORATE_CA_BUNDLE"] = str(ca_bundle)
    return env


def onboard(sandbox: str, manifest: Path, env: dict[str, str], runner: Runner, nemoclaw_bin: str) -> None:
    log(f"onboard {sandbox}: nemoclaw onboard --name {sandbox} --agents {manifest.name} "
        "--non-interactive (provider=custom → egress-proxy, tier=restricted)")
    started = time.monotonic()
    result = runner.run(
        [nemoclaw_bin, "onboard", "--name", sandbox, "--agents", str(manifest), "--non-interactive",
         "--yes-i-accept-third-party-software"],
        timeout=2400, env=env,
    )
    took = round(time.monotonic() - started)
    if not result.ok:
        tail = (result.stderr or result.stdout).strip().splitlines()[-12:]
        raise ConfigError(f"onboard {sandbox} failed after {took}s:\n" + "\n".join(tail))
    log(f"onboard {sandbox}: ready in {took}s")


def retire_legacy(name: str, runner: Runner, nemoclaw_bin: str) -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log(f"retire {name}: snapshot create --name pre-sg-{stamp}, then destroy")
    runner.run([nemoclaw_bin, name, "snapshot", "create", "--name", f"pre-sg-{stamp}"], timeout=600)
    runner.run([nemoclaw_bin, name, "destroy", "--yes"], timeout=600, check=True)


# --------------------------------------------------------------------------- seeding


def seed_sandbox(
    assignments: Assignments, sandbox: str, secret: bytes, runner: Runner, nemoclaw_bin: str,
    skills_dir: Path | None = None,
) -> list[str]:
    """Upload IDENTITY.md (signed agent marker) and the agent skill into every agent workspace."""
    skills_dir = skills_dir or DEPLOY_DIR / "skills"
    uploaded: list[str] = []
    targets: list[tuple[str, str, str | None]] = []  # (openclaw id, identity text, skill name)
    main_agent = assignments.main_agent(sandbox)
    if main_agent is None:
        targets.append(("main", render_head_identity(assignments, sandbox, secret), "sg-head"))
        for agent_id in assignments.sandbox_agents(sandbox):
            spec = assignments.agents[agent_id]
            targets.append((agent_id, render_identity(assignments, agent_id, secret), spec.skill))
    else:
        spec = assignments.agents[main_agent]
        targets.append(("main", render_identity(assignments, main_agent, secret), spec.skill))
    with tempfile.TemporaryDirectory() as tmp:
        for openclaw_id, identity, skill in targets:
            workspace = workspace_path(openclaw_id)
            runner.run([nemoclaw_bin, sandbox, "exec", "--timeout", "30", "--", "mkdir", "-p",
                        f"{workspace}/skills"], timeout=90)
            local = Path(tmp) / f"IDENTITY-{openclaw_id}.md"
            local.write_text(identity, encoding="utf-8")
            runner.run([nemoclaw_bin, sandbox, "upload", str(local), f"{workspace}/IDENTITY.md"],
                       timeout=120, check=True)
            uploaded.append(f"{workspace}/IDENTITY.md")
            if skill and (skills_dir / skill).is_dir():
                if openclaw_id == "main":
                    runner.run([nemoclaw_bin, sandbox, "skill", "install", str(skills_dir / skill)],
                               timeout=300, check=True)
                else:
                    runner.run([nemoclaw_bin, sandbox, "upload", str(skills_dir / skill),
                                f"{workspace}/skills/{skill}"], timeout=300, check=True)
                uploaded.append(f"{workspace}/skills/{skill}")
    return uploaded


# --------------------------------------------------------------------------- orchestration


@dataclass
class BootstrapOptions:
    retire: tuple[str, ...] = LEGACY_SANDBOXES
    only: tuple[str, ...] = ()
    skip_mcp: bool = False
    mcp_fallback: bool = False
    dry_run: bool = False


def bootstrap(options: BootstrapOptions, runner: Runner | None = None) -> dict:
    runner = runner or SubprocessRunner(cwd=ROOT)
    assignments, routing, censors = load_assignments(), load_routing(), load_censors()
    problems = controller.plan  # noqa: F841 (keeps import graph obvious)
    from rfa_mas.nemoclaw.config import cross_check

    issues = cross_check(assignments, routing, censors)
    if issues:
        raise ConfigError("configuration cross-check failed: " + "; ".join(issues))
    nb = assignments.host.nemoclaw_bin
    report: dict = {"steps": [], "sandboxes": {}}

    version = check_nemoclaw(runner, nb)
    log(f"preflight: {version}")
    host_secrets = ensure_host_secrets(routing)
    secret = load_or_create_secret(ROOT / routing.proxy.marker_key_file)
    build = routing.backends["build"]
    if build.env_file:
        check_env_file(ROOT / build.env_file, build.credential_env or "NVIDIA_API_KEY")
    check_ollama(routing.aliases[routing.proxy.unattributed_alias].model, runner)
    ca_bundle = None if options.skip_mcp else ensure_tls(ROOT / routing.broker.tls_dir, assignments.host.lan_ip, runner)
    proxy_port = routing.proxy.bind.rsplit(":", 1)[1]
    health = check_health(f"http://127.0.0.1:{proxy_port}/healthz", "egress-proxy", attempts=5)
    log(f"preflight: egress-proxy healthy mode={health.get('mode')} aliases={health.get('aliases')}")
    report["steps"].append("preflight")

    manifests = write_manifests(assignments, DEPLOY_DIR / "agents")
    rendered = {
        name: controller.render_preset(name, assignments.host.lan_ip, SG_DIR / "rendered")
        for sb in assignments.sandboxes
        for name in assignments.sandbox_presets(sb, fallback=True)
    }
    observer = controller.Observer(runner, nb)
    live = observer.list_sandboxes()
    for legacy in options.retire:
        if legacy in live and legacy not in assignments.sandboxes:
            if options.dry_run:
                log(f"dry-run: would retire legacy sandbox {legacy}")
            else:
                retire_legacy(legacy, runner, nb)
                report["steps"].append(f"retired:{legacy}")
    env = onboard_env(routing, host_secrets, ca_bundle)
    for sandbox in assignments.ordered_sandboxes():
        if options.only and sandbox not in options.only:
            continue
        live = observer.list_sandboxes()
        if sandbox in live:
            log(f"onboard {sandbox}: exists, skipping")
        elif options.dry_run:
            log(f"dry-run: would onboard {sandbox}")
            continue
        else:
            onboard(sandbox, manifests[sandbox], env, runner, nb)
            report["steps"].append(f"onboarded:{sandbox}")
        observed = observer.observe([sandbox])
        mcp_url = None if options.skip_mcp else f"https://{assignments.host.lan_ip}:{routing.broker.bind.rsplit(':', 1)[1]}{routing.broker.path}"
        inputs = controller.PlanInputs(
            assignments, observed, rendered, manifests, nemoclaw_bin=nb,
            mcp_url=mcp_url, mcp_credential_env=routing.broker.credential_env,
            mcp_fallback=options.mcp_fallback,
        )
        actions = [a for a in controller.plan(inputs) if a.sandbox == sandbox]
        for action in actions:
            log(action.display())
        if options.dry_run:
            continue
        results = controller.apply(actions, runner, host_secrets.get)
        failed = [(a, r) for a, r in results if not r.ok]
        if failed:
            action, result = failed[0]
            if action.kind == "mcp-add" and not options.mcp_fallback:
                log(f"mcp-add failed for {sandbox}; retry with --mcp-fallback (REST preset) — "
                    f"{(result.stderr or result.stdout).strip()[-300:]}")
            raise ConfigError(f"{action.kind} failed on {sandbox}: {(result.stderr or result.stdout).strip()[-400:]}")
        uploaded = seed_sandbox(assignments, sandbox, secret, runner, nb)
        report["sandboxes"][sandbox] = {"actions": [a.kind for a in actions], "seeded": uploaded}
        log(f"seed {sandbox}: {len(uploaded)} files")
    report["steps"].append("done")
    return report


def teardown(runner: Runner | None = None, *, names: tuple[str, ...] = ()) -> list[str]:
    runner = runner or SubprocessRunner(cwd=ROOT)
    assignments = load_assignments()
    nb = assignments.host.nemoclaw_bin
    live = controller.Observer(runner, nb).list_sandboxes()
    destroyed = []
    for sandbox in reversed(assignments.ordered_sandboxes()):
        if names and sandbox not in names:
            continue
        if sandbox not in live:
            continue
        log(f"teardown {sandbox}: destroy --yes")
        result = runner.run([nb, sandbox, "destroy", "--yes"], timeout=600)
        if result.ok:
            destroyed.append(sandbox)
        else:
            log(f"teardown {sandbox}: destroy failed: {(result.stderr or result.stdout)[-200:]}")
    return destroyed


def status(runner: Runner | None = None) -> dict:
    runner = runner or SubprocessRunner(cwd=ROOT)
    assignments = load_assignments()
    nb = assignments.host.nemoclaw_bin
    observer = controller.Observer(runner, nb)
    observed = observer.observe(assignments.sandboxes)
    out = {}
    for sandbox in assignments.ordered_sandboxes():
        entry = {"exists": observed.exists(sandbox), "groups": assignments.sandboxes[sandbox].groups,
                 "privilege": assignments.sandbox_privilege(sandbox),
                 "agents_declared": assignments.sandbox_agents(sandbox)}
        if observed.exists(sandbox):
            policy = observed.policies[sandbox]
            keys = set(policy.get("network_policies") or {})
            entry.update({
                "presets_live": sorted(controller.custom_presets(policy)),
                "baseline_excluded": sorted(k for k in assignments.baseline_excludes if k not in keys),
                "baseline_present": sorted(k for k in assignments.baseline_excludes if k in keys),
                "agents_live": observed.agents[sandbox],
                "mcp_live": sorted(observed.mcp[sandbox]),
                "model": observed.sandboxes[sandbox].get("model"),
                "provider": observed.sandboxes[sandbox].get("provider"),
            })
        out[sandbox] = entry
    return out


def json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


__all__ = [
    "BootstrapOptions", "bootstrap", "teardown", "status", "seed_sandbox", "ensure_host_secrets",
    "ensure_tls", "check_env_file", "read_env_value", "onboard_env", "retire_legacy", "SG_DIR", "ROOT",
    "openclaw_agent_id", "extract_json", "CommandError", "os",
]
