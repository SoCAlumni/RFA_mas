"""NemoClaw security-group layer: declarative sandbox placement, censored egress routing, broker.

Everything here is driven by three files under ``deploy/nemoclaw``:

- ``assignments.yaml`` — agents → security groups → sandboxes (reconciled with the NemoClaw CLI)
- ``routing.yaml`` — channels → model aliases → backends, proxy/entry/broker listeners
- ``censors.yaml`` — censor profiles (regex → LLM, redact by default, fail-closed)

Rules that must not be crossable live in the OpenShell layer (presets, baseline excludes);
the agents.yaml/broker layers add granularity, mistake-proofing and audit only.
"""
