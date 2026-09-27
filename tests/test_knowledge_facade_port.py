"""The knowledge facade port is declared in four places; they must agree and avoid 8791
(RFA_module's head_stub owns 8791 and its run scripts kill any listener there)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from rfa_mas.cli import _parser

ROOT = Path(__file__).resolve().parents[1]
HEAD_STUB_PORT = 8791


def _cli_default() -> int:
    return _parser().parse_args(["knowledge-facade"]).port


def test_facade_port_agrees_across_cli_makefile_preset_and_skills() -> None:
    port = _cli_default()
    assert port != HEAD_STUB_PORT

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(rf"^KF_PORT \?= {port}$", makefile, re.M)
    assert "--port $(KF_PORT)" in makefile

    preset_path = ROOT / "deploy/nemoclaw/presets/sg-intranet-ro.yaml"
    preset = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
    endpoints = preset["network_policies"]["sg_intranet_ro"]["endpoints"]
    assert [e["port"] for e in endpoints] == [port]

    for skill in ("task-research", "task-benchmark"):
        text = (ROOT / "deploy/nemoclaw/skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        ports = {int(p) for p in re.findall(r"http://[\d.]+:(\d+)/tasks", text)}
        assert ports == {port}, skill
