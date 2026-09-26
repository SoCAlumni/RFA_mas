#!/usr/bin/env python
"""E2E-05 real local OpenShell allow/deny matrix (P1-007C).

Re-runnable orchestrator for the E2E-05 security gate on a local, standalone OpenShell
gateway (VM compute driver). Everything is synthetic: two tiny in-process HTTP servers on
the host's non-loopback IP, a host-originated sentinel baked into the stand-in rootfs, and
an approved CPU metric script. No key, token, .env value or production resource is used.

Scope label: real OpenShell, local standalone. This is not the teammate runtime/identity
integration (P1-008B) and not NemoClaw (P1-007A).

Classification (per attempt):
  allowed           the operation succeeded (and, for network, an OpenShell ALLOWED record)
  denied_by_policy  the operation failed AND OpenShell policy evidence attributes the
                    failure: a matching OCSF DENIED record for network, or, for filesystem,
                    a policy differential (the same object/operation succeeds in a control
                    sandbox whose policy differs only in that path; Landlock in OpenShell
                    0.1.1 emits no per-access audit event)
  inconclusive      anything else: missing file, endpoint down, generic error without a
                    policy record, or a failure that Unix permissions alone also explain

Usage (gateway must already be running and connected):
  .venv/bin/python scripts/openshell_e2e05.py --rootfs /abs/rfa-e2e05-rootfs.tar --out /abs/dir
Exit codes: 0 every row met its expectation, 1 at least one row did not, 2 not_run.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy" / "openshell"
HOST_SENTINEL = DEPLOY / "image" / "srv" / "rfa-private" / "SENTINEL_E2E05.txt"
GUEST_SENTINEL = "/srv/rfa-private/SENTINEL_E2E05.txt"
GUEST_UNAPPROVED_TOOL = "/opt/rfa-unapproved/unapproved_tool.sh"
GUEST_WORKDIR = "/sandbox/workdir"  # "sandbox upload <dir> /sandbox" keeps the directory name
CANARY = "CANARY_SENTINEL_E2E05"
EXPECTED_OPENSHELL = "0.1.1"
PYTHON_IDENTITY = "/usr/local/bin/python3.12"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
OCSF_LINE = re.compile(r"^\[(?P<ts>\d+\.\d+)\] \[sandbox\] \[OCSF \] \[ocsf\] (?P<body>.*)$")
SAFE_CODES = {
    "network_l4": "policy_denied.network",
    "network_l7": "policy_denied.request",
    "binary_identity": "policy_denied.binary_identity",
    "filesystem": "policy_denied.filesystem",
    "execution": "policy_denied.execution",
}
LABEL = (
    "real OpenShell, local standalone; teammate runtime/identity (P1-008B) "
    "and NemoClaw (P1-007A) not covered"
)
NOTE_ANCESTOR = (
    "informational: OpenShell binary identity also accepts descendants of an approved binary"
)
NOTE_WORKDIR_EXEC = (
    "informational: OpenShell 0.1.1 path policy does not forbid execution in a writable path"
)
NOTE_SENTINEL = (
    "policy differential: research-control reads the same file (sha256 = host), mode 0644"
)
NOTE_HOST_PATH = "the microVM does not mount the host filesystem; absence is not a policy denial"

# Runs inside the sandbox with python3 -c. Returns one JSON line; never returns file or
# response bodies, only status, errno, sha256 and sizes.
GUEST_RUNNER = r"""
import hashlib, json, os, subprocess, sys, time, urllib.error, urllib.request
spec = json.loads(sys.argv[1]); op = spec["op"]; out = {"op": op}
t = time.perf_counter()
def err(e):
    no = getattr(e, "errno", None)
    if no is None and isinstance(getattr(e, "reason", None), OSError):
        no = e.reason.errno
    return {"ok": False, "exc": type(e).__name__, "errno": no}
try:
    if op == "http_get":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(spec["url"], timeout=10) as r:
                b = r.read()
                digest = hashlib.sha256(b).hexdigest()
                out.update(ok=True, status=r.status, sha256=digest, bytes=len(b))
        except urllib.error.HTTPError as e:
            body = e.read(4096)
            try:
                doc = json.loads(body); detail = {k: doc.get(k) for k in ("error", "layer")}
            except Exception:
                detail = {}
            out.update(ok=False, status=e.code, exc="HTTPError", policy=detail)
    elif op == "bash_tcp":
        target = "/dev/tcp/%s/%d" % (spec["host"], spec["port"])
        req = "GET %s HTTP/1.0\r\nHost: %s\r\n\r\n" % (spec["path"], spec["host"])
        cmd = 'exec 3<>%s && printf "%s" >&3 && head -c 12 <&3' % (target, req)
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=15)
        ok = p.returncode == 0 and p.stdout.startswith("HTTP/")
        out.update(ok=ok, rc=p.returncode, eacces="Permission denied" in p.stderr)
    elif op == "read":
        with open(spec["path"], "rb") as f:
            b = f.read()
        out.update(ok=True, sha256=hashlib.sha256(b).hexdigest(), bytes=len(b))
    elif op == "stat":
        st = os.stat(spec["path"])
        out.update(ok=True, mode=oct(st.st_mode & 0o7777), uid=st.st_uid)
    elif op == "write":
        with open(spec["path"], "w") as f:
            f.write("synthetic E2E-05 write probe\n")
        out.update(ok=True)
    elif op == "exec":
        p = subprocess.run(spec["argv"], capture_output=True, text=True, timeout=15)
        marker = spec["marker"] in p.stdout if spec.get("marker") else None
        out.update(ok=p.returncode == 0, rc=p.returncode, marker=marker)
    elif op == "compute":
        argv = [sys.executable, spec["script"]]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        res = json.loads(p.stdout) if p.returncode == 0 else None
        written = os.path.exists(spec["result_file"])
        out.update(ok=p.returncode == 0, rc=p.returncode, result=res, result_file=written)
    elif op == "copy_exec":
        import shutil
        shutil.copy(spec["src"], spec["dst"]); os.chmod(spec["dst"], 0o755)
        p = subprocess.run([spec["dst"]], capture_output=True, text=True, timeout=15)
        out.update(ok=p.returncode == 0, rc=p.returncode)
    elif op == "which":
        out.update(ok=True, found=bool(__import__("shutil").which(spec["name"])))
    else:
        out.update(ok=False, exc="UnknownOp")
except Exception as e:
    out.update(err(e))
out["op_ms"] = round((time.perf_counter() - t) * 1000, 3)
print(json.dumps(out))
"""


# ------------------------------------------------------------------ classification (pure)
def classify_network(expect: str, attempt: dict[str, Any], log: str | None, health_ok: bool) -> str:
    """Network rows. A deny needs a failed attempt plus a matching OpenShell DENIED record."""
    if attempt.get("ok"):
        if expect == "allow" and log is None:
            return "inconclusive"
        return "allowed"
    if not health_ok:
        return "inconclusive"
    if log is None:
        return "inconclusive"
    return "denied_by_policy"


def classify_filesystem(attempt: dict[str, Any], control_proved: bool) -> str:
    """Filesystem/exec rows. EACCES counts only with a successful control differential."""
    if attempt.get("ok"):
        return "allowed"
    if attempt.get("errno") == 2 or attempt.get("exc") == "FileNotFoundError":
        return "inconclusive"
    if attempt.get("errno") in (1, 13) and control_proved:
        return "denied_by_policy"
    return "inconclusive"


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "median_ms": round(statistics.median(values), 1),
        "min_ms": round(min(values), 1),
        "max_ms": round(max(values), 1),
    }


# ------------------------------------------------------------------ host helpers
def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def detect_host_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1; UDP connect sends no packet
        ip = probe.getsockname()[0]
    finally:
        probe.close()
    if ip.startswith("127.") or ip == "0.0.0.0":
        raise RuntimeError("no non-loopback host IPv4 address")
    return ip


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    hits: list[str]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        self.hits.append(self.path)


def start_server(ip: str, directory: Path) -> tuple[http.server.ThreadingHTTPServer, list[str]]:
    hits: list[str] = []
    handler = type("Handler", (_QuietHandler,), {"hits": hits})
    server = http.server.ThreadingHTTPServer(
        (ip, 0), functools.partial(handler, directory=str(directory))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


def host_get(url: str) -> tuple[int | None, float]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t = time.perf_counter()
    try:
        with opener.open(url, timeout=5) as response:
            response.read()
            return response.status, (time.perf_counter() - t) * 1000
    except urllib.error.HTTPError as exc:
        return exc.code, (time.perf_counter() - t) * 1000
    except OSError:
        return None, (time.perf_counter() - t) * 1000


@dataclass
class OpenShell:
    binary: str = "openshell"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(self, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        argv = [self.binary, *args]
        t = time.perf_counter()
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        self.calls.append(
            {
                "argv": argv,
                "exit": proc.returncode,
                "ms": round((time.perf_counter() - t) * 1000, 1),
            }
        )
        return proc

    def create(self, name: str, rootfs: Path, policy: Path) -> float:
        t = time.perf_counter()
        proc = self.run(
            "sandbox",
            "create",
            "--name",
            name,
            "--from",
            str(rootfs),
            "--policy",
            str(policy),
            "--no-auto-providers",
            "--detach",
            "--",
            "sleep",
            "3600",
            timeout=600,
        )
        ms = (time.perf_counter() - t) * 1000
        if proc.returncode != 0:
            raise RuntimeError(
                f"sandbox create failed for {name}: {ANSI.sub('', proc.stderr)[-400:]}"
            )
        return ms

    def identity(self, name: str) -> dict[str, Any]:
        proc = self.run("sandbox", "get", name, "--output", "json")
        doc = json.loads(proc.stdout)
        admission = doc.get("configuration_admission") or {}
        policy = doc.get("policy") or {}
        return {
            "sandbox_name": name,
            "sandbox_id": doc.get("id"),
            "phase": doc.get("phase"),
            "policy_source": doc.get("policy_source"),
            "policy_version": admission.get("policy_version"),
            "policy_hash": admission.get("policy_hash"),
            "effective_filesystem_policy": policy.get("filesystem_policy"),
            "landlock": policy.get("landlock"),
            "network_policy_names": sorted((policy.get("network_policies") or {}).keys()),
        }

    def guest(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        t = time.perf_counter()
        proc = self.run(
            "sandbox",
            "exec",
            "-n",
            name,
            "--no-tty",
            "--no-login-shell",
            "--",
            "python3",
            "-c",
            GUEST_RUNNER,
            json.dumps(spec),
            timeout=120,
        )
        wall = (time.perf_counter() - t) * 1000
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            result = {"ok": False, "exc": "RunnerOutputUnparsable"}
        result.update(
            exec_wall_ms=round(wall, 1), exec_exit=proc.returncode, t_start=t0, t_end=time.time()
        )
        return result

    def upload(self, name: str, local: Path, dest: str) -> float:
        t = time.perf_counter()
        proc = self.run("sandbox", "upload", "--no-git-ignore", name, str(local), dest)
        if proc.returncode != 0:
            raise RuntimeError(f"upload failed for {name}")
        return (time.perf_counter() - t) * 1000

    def guest_bash_tcp(self, name: str, host: str, port: int, path: str) -> dict[str, Any]:
        """Run bash directly as the exec command (no approved ancestor process)."""
        request = f"GET {path} HTTP/1.0\\r\\nHost: {host}\\r\\n\\r\\n"
        script = (
            f's=$EPOCHREALTIME; if exec 3<>/dev/tcp/{host}/{port}; then printf "{request}" >&3;'
            ' head -c 12 <&3 | grep -q "^HTTP/" && r=ok || r=noresponse; else r=connect_failed; fi;'
            ' e=$EPOCHREALTIME; echo "RESULT=$r START=$s END=$e"'
        )
        t0 = time.time()
        t = time.perf_counter()
        proc = self.run(
            "sandbox",
            "exec",
            "-n",
            name,
            "--no-tty",
            "--no-login-shell",
            "--",
            "bash",
            "-c",
            script,
        )
        wall = (time.perf_counter() - t) * 1000
        match = re.search(r"RESULT=(\w+) START=([\d.]+) END=([\d.]+)", proc.stdout)
        result: dict[str, Any] = {"op": "bash_tcp_direct"}
        if match:
            result.update(
                ok=match[1] == "ok",
                outcome=match[1],
                op_ms=round((float(match[3]) - float(match[2])) * 1000, 3),
                eacces="Permission denied" in proc.stderr,
            )
        else:
            result.update(ok=False, outcome="unparsable", op_ms=None)
        result.update(
            exec_wall_ms=round(wall, 1), exec_exit=proc.returncode, t_start=t0, t_end=time.time()
        )
        return result

    def ocsf(self, name: str) -> list[tuple[float, str]]:
        proc = self.run("logs", name, "-n", "5000", "--source", "sandbox")
        lines = []
        for raw in ANSI.sub("", proc.stdout).splitlines():
            match = OCSF_LINE.match(raw.strip())
            if match:
                lines.append((float(match["ts"]), match["body"]))
        return lines

    def pending_rules(self, name: str) -> dict[str, int]:
        counts = {}
        for status in ("pending", "approved"):
            proc = self.run("rule", "get", name, "--status", status)
            counts[status] = len(re.findall(r"Status:\s*" + status, ANSI.sub("", proc.stdout)))
        return counts

    def delete(self, *names: str) -> None:
        if names:
            self.run("sandbox", "delete", *names, timeout=120)


def find_log(lines: list[tuple[float, str]], attempt: dict[str, Any], *needles: str) -> str | None:
    lo, hi = attempt["t_start"] - 1.0, attempt["t_end"] + 2.0
    for ts, body in lines:
        if lo <= ts <= hi and all(needle in body for needle in needles):
            return body
    return None


# ------------------------------------------------------------------ SupervisorBus (app layer)
def supervisor_bus_decisions() -> dict[str, Any]:
    sys.path.insert(0, str(REPO / "src"))
    from rfa_mas.application.workers import SupervisorBus
    from rfa_mas.errors import RfaError

    bus = SupervisorBus()
    cases = [
        ("paper_scout", "experiment_runner", "deny"),
        ("experiment_runner", "result_analyst", "deny"),
        ("paper_scout", "supervisor", "deliver"),
        ("supervisor", "experiment_runner", "deliver"),
        ("supervisor", "supervisor", "deny"),
    ]
    rows = []
    for sender, recipient, expect in cases:
        t = time.perf_counter()
        try:
            bus.send(sender, recipient, {"synthetic": "E2E-05 message"})
            decision, code = "delivered", None
        except RfaError as exc:
            decision, code = "denied", exc.code
        rows.append(
            {
                "sender": sender,
                "recipient": recipient,
                "expected": expect,
                "decision": decision,
                "code": code,
                "met": (decision == "delivered") == (expect == "deliver"),
                "latency_ms": round((time.perf_counter() - t) * 1000, 4),
            }
        )
    debate_lease_present = any(
        "DebateLease" in p.read_text(errors="ignore") for p in (REPO / "src").rglob("*.py")
    )
    return {
        "layer": "application SupervisorBus (in-process, no OpenShell involved)",
        "rows": rows,
        "delivered_log": [list(pair) for pair in bus.delivered],
        "debate_lease_construct_in_src": debate_lease_present,
    }


# ------------------------------------------------------------------ matrix
@dataclass
class Row:
    step: str
    role: str
    probe: str
    expected: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    classifications: list[str] = field(default_factory=list)
    evidence: list[str | None] = field(default_factory=list)
    safe_code: str | None = None
    note: str = ""

    def result(self) -> dict[str, Any]:
        observed = sorted(set(self.classifications))
        return {
            "step": self.step,
            "role": self.role,
            "probe": self.probe,
            "expected": self.expected,
            "observed": observed,
            "met": observed == [self.expected],
            "n": len(self.classifications),
            "safe_code": self.safe_code,
            "evidence_line": next((e for e in self.evidence if e), None),
            "note": self.note,
            "attempts": [
                {k: v for k, v in a.items() if k not in ("t_start", "t_end")} for a in self.attempts
            ],
        }


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = datetime.now(UTC)
    run_id = started.strftime("%m%d%H%M%S")
    shell = OpenShell(args.openshell)
    report: dict[str, Any] = {
        "scenario": "E2E-05",
        "task": "P1-007C",
        "run_id": run_id,
        "label": LABEL,
        "started_at": started.isoformat(),
    }
    version = shell.run("--version").stdout.strip()
    status = ANSI.sub("", shell.run("status").stdout)
    rootfs = Path(args.rootfs)
    reasons = []
    if EXPECTED_OPENSHELL not in version:
        reasons.append(f"openshell version {version!r} != {EXPECTED_OPENSHELL}")
    if "Connected" not in status:
        reasons.append("openshell gateway not connected")
    if not rootfs.is_file():
        reasons.append("rootfs tar missing (build with deploy/openshell/build_rootfs.sh)")
    if not HOST_SENTINEL.is_file():
        reasons.append("host sentinel missing")
    if reasons:
        report.update(status="not_run", reasons=reasons)
        return report

    host_ip = args.host_ip or detect_host_ip()
    out_dir = Path(args.out)
    policies_dir = out_dir / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    allowed_srv, allowed_hits = start_server(host_ip, DEPLOY / "endpoints" / "allowed")
    denied_srv, denied_hits = start_server(host_ip, DEPLOY / "endpoints" / "denied")
    a_port, d_port = allowed_srv.server_address[1], denied_srv.server_address[1]
    public_url = f"http://{host_ip}:{a_port}/public/R01_release_notice_v1.txt"
    internal_url = f"http://{host_ip}:{a_port}/internal/R02_roadmap.txt"
    denied_url = f"http://{host_ip}:{d_port}/index.txt"
    public_sha = sha256_file(
        DEPLOY / "endpoints" / "allowed" / "public" / "R01_release_notice_v1.txt"
    )

    def render(name: str) -> Path:
        text = (DEPLOY / "policies" / name).read_text()
        text = text.replace("{{HOST_IP}}", host_ip).replace("{{ALLOWED_PORT}}", str(a_port))
        target = policies_dir / name.replace(".tmpl", "")
        target.write_text(text)
        return target

    pol = {
        "research": render("research.yaml.tmpl"),
        "research_control": render("research-control.yaml.tmpl"),
        "execution": render("execution.yaml"),
        "execution_control": render("execution-control.yaml"),
    }
    sentinel_sha = sha256_file(HOST_SENTINEL)
    report["environment"] = {
        "openshell_version": version,
        "gateway_status": "Connected",
        "host": {"os": platform.platform(), "machine": platform.machine()},
        "repo_head": subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "rootfs": {
            "path": str(rootfs),
            "sha256": sha256_file(rootfs),
            "bytes": rootfs.stat().st_size,
        },
        "base_image": re.search(
            r"^FROM (\S+)", (DEPLOY / "image" / "Dockerfile").read_text(), re.M
        )[1],
        "policies_sha256": {k: sha256_file(v) for k, v in pol.items()},
        "endpoints": {"host_ip": host_ip, "allowed_port": a_port, "denied_port": d_port},
        "host_sentinel": {
            "path": str(HOST_SENTINEL.relative_to(REPO)),
            "exists": True,
            "sha256": sentinel_sha,
            "bytes": HOST_SENTINEL.stat().st_size,
        },
    }

    def health() -> bool:
        return all(host_get(u)[0] == 200 for u in (public_url, internal_url, denied_url))

    created: list[str] = []
    rows: dict[str, Row] = {}
    lat: dict[str, list[float]] = {
        k: []
        for k in (
            "cold_prep_research_ms",
            "cold_prep_execution_ms",
            "upload_workdir_ms",
            "warm_allowed_read_exec_ms",
            "warm_allowed_read_request_ms",
            "warm_compute_exec_ms",
            "deny_l7_request_ms",
            "deny_l4_connect_ms",
            "deny_binary_ms",
            "deny_fs_read_ms",
            "deny_fs_write_ms",
            "deny_exec_ms",
            "deny_exec_exec_ms",
            "host_direct_get_ms",
        )
    }
    report["preflight_health"] = {"all_200": health()}
    for _ in range(args.iterations * args.warm_repeats):
        code, ms = host_get(public_url)
        if code == 200:
            lat["host_direct_get_ms"].append(ms)

    def row(key: str, **kwargs: Any) -> Row:
        if key not in rows:
            rows[key] = Row(**kwargs)
        return rows[key]

    try:
        # ---- controls: prove the objects exist and Unix permissions allow the sandbox user
        # OpenShell sandbox names are limited to 19 characters.
        tag = f"e5-{run_id[-6:]}"
        rc_name, ec_name = f"{tag}-rc", f"{tag}-xc"
        shell.create(rc_name, rootfs, pol["research_control"])
        created.append(rc_name)
        shell.create(ec_name, rootfs, pol["execution_control"])
        created.append(ec_name)
        c_read = shell.guest(rc_name, {"op": "read", "path": GUEST_SENTINEL})
        c_stat = shell.guest(rc_name, {"op": "stat", "path": GUEST_SENTINEL})
        c_tool = shell.guest(
            ec_name,
            {"op": "exec", "argv": [GUEST_UNAPPROVED_TOOL], "marker": "UNAPPROVED_TOOL_RAN"},
        )
        c_tmp = shell.guest(ec_name, {"op": "write", "path": "/tmp/rfa-e2e05-escape.txt"})
        c_tmp_stat = shell.guest(ec_name, {"op": "stat", "path": "/tmp"})
        sentinel_control = bool(
            c_read.get("ok")
            and c_read.get("sha256") == sentinel_sha
            and c_stat.get("mode") == "0o644"
        )
        tool_control = bool(c_tool.get("ok") and c_tool.get("marker"))
        tmp_control = bool(c_tmp.get("ok") and c_tmp_stat.get("mode") == "0o1777")
        report["controls"] = {
            "research_control": {
                **shell.identity(rc_name),
                "sentinel_read_ok": c_read.get("ok"),
                "sentinel_sha256_matches_host": c_read.get("sha256") == sentinel_sha,
                "sentinel_mode": c_stat.get("mode"),
                "sentinel_uid": c_stat.get("uid"),
                "proves": sentinel_control,
            },
            "execution_control": {
                **shell.identity(ec_name),
                "unapproved_tool_ran": c_tool.get("marker"),
                "tmp_write_ok": c_tmp.get("ok"),
                "tmp_mode": c_tmp_stat.get("mode"),
                "proves_tool": tool_control,
                "proves_tmp": tmp_control,
            },
        }
        shell.delete(rc_name, ec_name)
        created = [n for n in created if n not in (rc_name, ec_name)]

        report["sandboxes"] = []
        for i in range(1, args.iterations + 1):
            # ---------------- research role (paper_scout stand-in)
            name = f"{tag}-r{i}"
            lat["cold_prep_research_ms"].append(shell.create(name, rootfs, pol["research"]))
            created.append(name)
            ident = shell.identity(name)
            attempts: dict[str, list[dict[str, Any]]] = {
                k: [] for k in ("R1", "R2", "R3", "R4", "R4b", "R5", "R6", "R7")
            }
            health_pre = health()
            hits_before = (len(allowed_hits), len(denied_hits))
            for _ in range(args.warm_repeats):
                attempts["R1"].append(shell.guest(name, {"op": "http_get", "url": public_url}))
            for _ in range(args.deny_repeats):
                attempts["R2"].append(shell.guest(name, {"op": "http_get", "url": internal_url}))
                attempts["R3"].append(shell.guest(name, {"op": "http_get", "url": denied_url}))
                attempts["R4"].append(
                    shell.guest_bash_tcp(name, host_ip, a_port, "/public/R01_release_notice_v1.txt")
                )
                attempts["R5"].append(shell.guest(name, {"op": "read", "path": GUEST_SENTINEL}))
            attempts["R4b"].append(
                shell.guest(
                    name,
                    {
                        "op": "bash_tcp",
                        "host": host_ip,
                        "port": a_port,
                        "path": "/public/R01_release_notice_v1.txt",
                    },
                )
            )
            attempts["R6"].append(shell.guest(name, {"op": "read", "path": str(HOST_SENTINEL)}))
            rules = shell.pending_rules(name)
            attempts["R7"].append(shell.guest(name, {"op": "http_get", "url": denied_url}))
            cli_in_guest = shell.guest(name, {"op": "which", "name": "openshell"})
            health_post = health()
            internal_hits = sum(
                1 for p in allowed_hits[hits_before[0] :] if p.startswith("/internal/")
            )
            denied_server_hits = len(denied_hits) - hits_before[1]
            time.sleep(2)
            logs = shell.ocsf(name)
            h_ok = health_pre and health_post
            ip_a, ip_d = f"{host_ip}:{a_port}", f"{host_ip}:{d_port}"
            for a in attempts["R1"]:
                ev = find_log(logs, a, "HTTP:GET", "ALLOWED", f"{ip_a}/public/")
                ok = bool(a.get("ok") and a.get("sha256") == public_sha)
                r = row(
                    "R1",
                    step="R1",
                    role="research",
                    probe="GET allowed public doc (python3.12)",
                    expected="allowed",
                )
                r.attempts.append(a)
                r.evidence.append(ev)
                r.classifications.append(classify_network("allow", {**a, "ok": ok}, ev, h_ok))
                lat["warm_allowed_read_exec_ms"].append(a["exec_wall_ms"])
                lat["warm_allowed_read_request_ms"].append(a["op_ms"])
            for key, probe, needles, safe, bucket in (
                (
                    "R2",
                    "GET /internal/ on the allowed host:port (path not in policy)",
                    ("HTTP:GET", "DENIED", f"{ip_a}/internal/"),
                    "network_l7",
                    "deny_l7_request_ms",
                ),
                (
                    "R3",
                    "GET a second host port not in policy",
                    ("NET:OPEN", "DENIED", f"-> {ip_d}", "transparent_tcp_policy_denied"),
                    "network_l4",
                    "deny_l4_connect_ms",
                ),
                (
                    "R4",
                    "unapproved binary (bash) to the allowed endpoint",
                    ("NET:OPEN", "DENIED", "/usr/bin/bash", f"-> {ip_a}"),
                    "binary_identity",
                    "deny_binary_ms",
                ),
            ):
                r = row(key, step=key, role="research", probe=probe, expected="denied_by_policy")
                r.safe_code = SAFE_CODES[safe]
                for a in attempts[key]:
                    ev = find_log(logs, a, *needles)
                    r.attempts.append(a)
                    r.evidence.append(ev)
                    r.classifications.append(classify_network("deny", a, ev, h_ok))
                    if a.get("op_ms") is not None:
                        lat[bucket].append(a["op_ms"])
            r = rows["R2"]
            r.note = f"server saw {internal_hits} /internal/ requests during this sandbox"
            rows["R3"].note = f"denied server saw {denied_server_hits} requests during this sandbox"
            rows["R4"].note = "bash launched directly by sandbox exec (no approved ancestor)"
            r = row(
                "R4b",
                step="R4b",
                role="research",
                probe="bash spawned by the approved python3.12 to the allowed endpoint",
                expected="allowed",
                note=NOTE_ANCESTOR,
            )
            for a in attempts["R4b"]:
                ev = find_log(logs, a, "NET:OPEN", "ALLOWED", "/usr/bin/bash", f"-> {ip_a}")
                r.attempts.append(a)
                r.evidence.append(ev)
                r.classifications.append(classify_network("allow", a, ev, h_ok))
            r = row(
                "R5",
                step="R5",
                role="research",
                probe=f"read host-originated sentinel at {GUEST_SENTINEL}",
                expected="denied_by_policy",
                note=NOTE_SENTINEL,
            )
            r.safe_code = SAFE_CODES["filesystem"]
            for a in attempts["R5"]:
                r.attempts.append(a)
                r.evidence.append(
                    f"errno={a.get('errno')}; path absent from effective policy; "
                    f"control proved={sentinel_control}"
                )
                r.classifications.append(classify_filesystem(a, sentinel_control))
                lat["deny_fs_read_ms"].append(a["op_ms"])
            r = row(
                "R6",
                step="R6",
                role="research",
                probe="read the sentinel by its host absolute path",
                expected="inconclusive",
                note=NOTE_HOST_PATH,
            )
            for a in attempts["R6"]:
                r.attempts.append(a)
                r.evidence.append(f"errno={a.get('errno')} ({a.get('exc')})")
                r.classifications.append(classify_filesystem(a, False))
            r = row(
                "R7",
                step="R7",
                role="research",
                probe="retry a denied endpoint after the policy advisor drafted proposals",
                expected="denied_by_policy",
            )
            r.safe_code = SAFE_CODES["network_l4"]
            for a in attempts["R7"]:
                ev = find_log(logs, a, "NET:OPEN", "DENIED", f"-> {ip_d}")
                r.attempts.append(a)
                r.evidence.append(ev)
                cls = classify_network("deny", a, ev, h_ok)
                if rules.get("approved"):
                    cls = "inconclusive"
                r.classifications.append(cls)
            r.note = (
                f"proposals pending={rules.get('pending')} approved={rules.get('approved')} "
                f"(approval mode manual); openshell CLI inside sandbox={cli_in_guest.get('found')}"
            )
            report["sandboxes"].append(
                {
                    **ident,
                    "role": "research",
                    "iteration": i,
                    "health_pre": health_pre,
                    "health_post": health_post,
                    "ocsf_lines": len(logs),
                }
            )
            shell.delete(name)
            created.remove(name)

            # ---------------- execution role (experiment_runner stand-in)
            name = f"{tag}-x{i}"
            prep = shell.create(name, rootfs, pol["execution"])
            created.append(name)
            up = shell.upload(name, DEPLOY / "workdir", "/sandbox")
            lat["cold_prep_execution_ms"].append(prep + up)
            lat["upload_workdir_ms"].append(up)
            ident = shell.identity(name)
            e: dict[str, list[dict[str, Any]]] = {
                k: [] for k in ("E1", "E2", "E3", "E4", "E5", "E6")
            }
            health_pre = health()
            for _ in range(args.warm_repeats):
                e["E1"].append(
                    shell.guest(
                        name,
                        {
                            "op": "compute",
                            "script": f"{GUEST_WORKDIR}/metric_compare.py",
                            "result_file": f"{GUEST_WORKDIR}/result.json",
                        },
                    )
                )
            for _ in range(args.deny_repeats):
                e["E2"].append(
                    shell.guest(name, {"op": "write", "path": "/tmp/rfa-e2e05-escape.txt"})
                )
                e["E3"].append(
                    shell.guest(
                        name,
                        {
                            "op": "exec",
                            "argv": [GUEST_UNAPPROVED_TOOL],
                            "marker": "UNAPPROVED_TOOL_RAN",
                        },
                    )
                )
                e["E4"].append(shell.guest(name, {"op": "http_get", "url": public_url}))
            e["E5"].append(shell.guest(name, {"op": "write", "path": "/etc/rfa-e2e05.txt"}))
            e["E6"].append(
                shell.guest(
                    name,
                    {
                        "op": "copy_exec",
                        "src": "/usr/bin/true",
                        "dst": f"{GUEST_WORKDIR}/true_copy",
                    },
                )
            )
            health_post = health()
            time.sleep(2)
            logs = shell.ocsf(name)
            h_ok = health_pre and health_post
            expected_result = {
                "accuracy_delta_pts": -0.2,
                "compared": ["R03", "R04"],
                "latency_change_pct": -18.0,
                "latency_delta_ms": -1.8,
            }
            r = row(
                "E1",
                step="E1",
                role="execution",
                probe=f"approved CPU metric compare in {GUEST_WORKDIR}",
                expected="allowed",
            )
            for a in e["E1"]:
                ok = bool(
                    a.get("ok") and a.get("result") == expected_result and a.get("result_file")
                )
                r.attempts.append(a)
                r.evidence.append(f"result={a.get('result')}")
                r.classifications.append("allowed" if ok else "inconclusive")
                lat["warm_compute_exec_ms"].append(a["exec_wall_ms"])
            for key, probe, proved, safe, bucket, note in (
                (
                    "E2",
                    "write /tmp (world-writable 1777, outside approved workdir)",
                    tmp_control,
                    "filesystem",
                    "deny_fs_write_ms",
                    "policy differential: execution-control writes the same path",
                ),
                (
                    "E3",
                    f"execute {GUEST_UNAPPROVED_TOOL} (0755, outside approved paths)",
                    tool_control,
                    "execution",
                    "deny_exec_ms",
                    "policy differential: execution-control runs the same tool",
                ),
            ):
                r = row(
                    key,
                    step=key,
                    role="execution",
                    probe=probe,
                    expected="denied_by_policy",
                    note=note,
                )
                r.safe_code = SAFE_CODES[safe]
                for a in e[key]:
                    r.attempts.append(a)
                    r.evidence.append(
                        f"errno={a.get('errno')}; path absent from effective policy; "
                        f"control proved={proved}"
                    )
                    r.classifications.append(classify_filesystem(a, proved))
                    lat[bucket].append(a["op_ms"])
                    if key == "E3":
                        lat["deny_exec_exec_ms"].append(a["exec_wall_ms"])
            r = row(
                "E4",
                step="E4",
                role="execution",
                probe="network egress from the execution role (no network rules)",
                expected="denied_by_policy",
            )
            r.safe_code = SAFE_CODES["network_l4"]
            for a in e["E4"]:
                ev = find_log(
                    logs, a, "NET:OPEN", "DENIED", PYTHON_IDENTITY, f"-> {host_ip}:{a_port}"
                )
                r.attempts.append(a)
                r.evidence.append(ev)
                r.classifications.append(classify_network("deny", a, ev, h_ok))
            r = row(
                "E5",
                step="E5",
                role="execution",
                probe="write /etc (root-owned 0755)",
                expected="inconclusive",
                note="Unix permissions also deny; not attributable to OpenShell policy",
            )
            for a in e["E5"]:
                r.attempts.append(a)
                r.evidence.append(f"errno={a.get('errno')}")
                r.classifications.append(classify_filesystem(a, False))
            r = row(
                "E6",
                step="E6",
                role="execution",
                probe="execute a copied binary inside the writable workdir",
                expected="allowed",
                note=NOTE_WORKDIR_EXEC,
            )
            for a in e["E6"]:
                r.attempts.append(a)
                r.evidence.append(f"rc={a.get('rc')}")
                r.classifications.append("allowed" if a.get("ok") else "inconclusive")
            report["sandboxes"].append(
                {
                    **ident,
                    "role": "execution",
                    "iteration": i,
                    "health_pre": health_pre,
                    "health_post": health_post,
                    "ocsf_lines": len(logs),
                }
            )
            shell.delete(name)
            created.remove(name)
    finally:
        if created:
            shell.delete(*created)
        allowed_srv.shutdown()
        denied_srv.shutdown()

    report["matrix"] = [rows[k].result() for k in sorted(rows, key=lambda k: (k[0] != "R", k))]
    report["supervisor_bus"] = supervisor_bus_decisions()
    report["latency"] = {k: summarize(v) for k, v in lat.items()}
    report["attempt_policy"] = {
        "warm_repeats": args.warm_repeats,
        "deny_repeats": args.deny_repeats,
        "iterations": args.iterations,
        "automatic_retries": 0,
    }
    report["openshell_calls"] = len(shell.calls)
    met = all(r["met"] for r in report["matrix"]) and all(
        r["met"] for r in report["supervisor_bus"]["rows"]
    )
    serialized = json.dumps(report, default=str)
    report["canary_absent_from_report"] = CANARY not in serialized
    report["status"] = "passed" if met and report["canary_absent_from_report"] else "failed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def matrix_markdown(report: dict[str, Any]) -> str:
    lines = [
        "| step | role | probe | expected | observed | n | evidence line |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report.get("matrix", []):
        ev = (r["evidence_line"] or "").replace("|", "/")[:160]
        lines.append(
            f"| {r['step']} | {r['role']} | {r['probe']} | {r['expected']} "
            f"| {', '.join(r['observed'])} | {r['n']} | {ev} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rootfs", default=os.environ.get("RFA_OPENSHELL_ROOTFS"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--host-ip", default=None)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warm-repeats", type=int, default=5)
    parser.add_argument("--deny-repeats", type=int, default=3)
    parser.add_argument("--openshell", default=shutil.which("openshell") or "openshell")
    args = parser.parse_args()
    if not args.rootfs:
        print(
            json.dumps(
                {"status": "not_run", "reasons": ["--rootfs or RFA_OPENSHELL_ROOTFS is required"]}
            )
        )
        return 2
    args.out = args.out or tempfile.mkdtemp(prefix="rfa-e2e05-")
    report = run(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    (out / "matrix.md").write_text(matrix_markdown(report))
    print(
        json.dumps(
            {"status": report.get("status"), "out": str(out), "reasons": report.get("reasons")}
        )
    )
    return {"passed": 0, "failed": 1}.get(report.get("status"), 2)


if __name__ == "__main__":
    raise SystemExit(main())
