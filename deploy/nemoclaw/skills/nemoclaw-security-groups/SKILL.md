---
name: nemoclaw-security-groups
description: Operate NemoClaw sandboxes as security groups from three declarations (assignments.yaml, routing.yaml, censors.yaml). Use to plan/apply policy presets, add or move task agents, switch the inference route mode, review blocked network requests, or read the audit log.
---

# NemoClaw security groups (controller skill)

All operations go through `scripts/sg.sh <command>` (a wrapper for
`python -m rfa_mas.nemoclaw` run from the repository root). The controller only issues
NemoClaw CLI verbs: `policy add --from-file`, `policy remove`, `policy exclude`, `agents apply`,
`mcp add`, `policy explain --write`, `snapshot`, `logs`, `status`. It never runs
`openshell policy set`.

| Task | Command |
| --- | --- |
| Check the three declarations | `scripts/sg.sh validate` |
| Preview what would change on live sandboxes | `scripts/sg.sh plan` |
| Reconcile live sandboxes to the declarations | `scripts/sg.sh apply` |
| Per-sandbox live view (presets, excluded baseline keys, agents, MCP) | `scripts/sg.sh status` |
| Verify creation-locked static policy sections | `scripts/sg.sh verify-baseline` |
| Refresh the agent-facing policy context (`POLICY.md`) | `scripts/sg.sh explain` |
| Add a task agent | edit `agents:` in `deploy/nemoclaw/assignments.yaml`, then `scripts/sg.sh apply` |
| Move an agent between security groups | `scripts/sg.sh relocate <agent> --to-groups <g1,g2> [--wipe]` |
| Blocked network requests | `scripts/sg.sh requests sync`, `... list`, `... approve <id> --reason "..."`, `... deny <id>` |
| Switch the gateway route mode (no restart) | `scripts/sg.sh switch-route rfa-internal` (kill switch) / `rfa-auto` |
| Read the audit ledger | `scripts/sg.sh audit --limit 50 [--kind inference]` or open `http://127.0.0.1:8799/audit/` |

Rules of thumb:

- A sandbox is one combination of security groups; agents with the same group set share it.
- Demotion (moving to a lower-privilege group) scans the workspace with the external censor
  profile and carries only redacted copies; promotion carries state unchanged.
- Approving a blocked request creates a reviewed preset `sg-approved-<id>` for exactly that
  host, port, method, path and binary.
