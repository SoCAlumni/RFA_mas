"""Approved CPU-only metric comparison for the E2E-05 execution role (synthetic data).

Reads benchmarks.json next to this file and writes result.json in the same directory.
No network, no subprocess, no path outside the approved workdir.
"""

import json
from pathlib import Path

here = Path(__file__).resolve().parent
runs = {run["id"]: run for run in json.loads((here / "benchmarks.json").read_text())["runs"]}
a, b = runs["R03"], runs["R04"]
result = {
    "compared": ["R03", "R04"],
    "latency_delta_ms": round(b["latency_ms"] - a["latency_ms"], 3),
    "latency_change_pct": round((b["latency_ms"] - a["latency_ms"]) / a["latency_ms"] * 100, 2),
    "accuracy_delta_pts": round(b["accuracy_pct"] - a["accuracy_pct"], 3),
}
(here / "result.json").write_text(json.dumps(result, sort_keys=True) + "\n")
print(json.dumps(result, sort_keys=True))
