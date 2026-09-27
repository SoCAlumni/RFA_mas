# NemoClaw security-group operating layer. Targets work from a clean macOS host with
# NemoClaw 0.0.124 + OpenShell 0.0.116 (Colima/Docker), uv and openssl installed. Inference is hosted-only
# (build.nvidia.com, NVIDIA_API_KEY in .env.dev); no local LLM.
#
#   make bootstrap   preflight → egress-proxy/broker up → retire rfa-demo → onboard 4 sandboxes → reconcile → seed
#   make demo        run demo/01..10 except 06 (replay by default; DEMO_MODE=live for live runs)
#   make mock-e2e    desk(C)/approval(A) mocks against /ask: fake agents by default, MOCK_FLAGS="--ask-url http://127.0.0.1:8799" for live
#   make teardown    destroy the declared sandboxes and stop the host services
#
# All NemoClaw commands are issued by `python -m rfa_mas.nemoclaw` (never `openshell policy set`).

SHELL := /bin/bash
export PATH := $(HOME)/.hermes/node/bin:$(HOME)/.local/bin:$(PATH)
# Colima: the NemoClaw-managed OpenShell gateway needs DOCKER_HOST, otherwise it looks for /var/run/docker.sock
ifeq ($(origin DOCKER_HOST), undefined)
  ifneq ($(wildcard $(HOME)/.colima/default/docker.sock),)
    export DOCKER_HOST := unix://$(HOME)/.colima/default/docker.sock
  endif
endif
UV ?= uv run --offline --frozen
SG := $(UV) python -m rfa_mas.nemoclaw
DEMO_MODE ?= replay
SG_DIR := .local/sg
SERVE_PID := $(SG_DIR)/serve.pid
SERVE_LOG := $(SG_DIR)/serve.log
MCP_FLAGS ?=
MOCK_FLAGS ?= --fake-agents

.PHONY: help bootstrap demo teardown serve serve-stop status plan apply test validate render kb-seed \
        verify-baseline demo-01 demo-02 demo-03 demo-04 demo-05 demo-07 demo-08 demo-09 demo-10 knowledge-facade knowledge-facade-stop \
        mock-e2e mock-approval openapi

help:
	@sed -n '2,9p' Makefile

validate:
	$(SG) validate

render:
	$(SG) render

test:
	$(UV) python -m pytest -q -p no:cacheprovider tests/test_sg_controller.py tests/test_censor.py tests/test_broker.py tests/test_egress_proxy.py tests/test_sg_ops.py tests/test_entry.py tests/test_ask.py tests/test_mock_e2e.py tests/test_team_spawn.py

# /ask contract: Pydantic/FastAPI → docs/api/ask.openapi.{json,yaml} (committed; tests/test_ask.py checks it is current)
openapi:
	$(UV) python scripts/export_openapi.py ask

# ---- mocks (desk C / approval A) -----------------------------------------------------------
# Default: in-process fake head/tasks/censor (no sandbox, no model) — contract + feedback-loop check only.
# Live:    make mock-e2e MOCK_FLAGS="--ask-url http://127.0.0.1:8799"   (needs `make serve`, RFA_ASK_TOKEN in .env.dev)
mock-e2e:
	$(UV) python tools/mock/run_e2e.py $(MOCK_FLAGS)

# Manual-decision approval mock for demos: tools/mock/rfa-mock approve|reject <id> --reason ...
mock-approval:
	$(UV) python tools/mock/approval.py serve --port 8811

# ---- host services -------------------------------------------------------------------------

serve:
	@mkdir -p $(SG_DIR)
	@if [ -f $(SERVE_PID) ] && kill -0 $$(cat $(SERVE_PID)) 2>/dev/null; then \
	  echo "serve already running (pid $$(cat $(SERVE_PID)))"; \
	else \
	  nohup $(SG) serve > $(SERVE_LOG) 2>&1 & echo $$! > $(SERVE_PID); \
	  echo "serve started (pid $$(cat $(SERVE_PID))) log=$(SERVE_LOG)"; \
	fi
	@for i in $$(seq 1 30); do curl -sf http://127.0.0.1:8797/healthz >/dev/null && break; sleep 1; done; \
	curl -sf http://127.0.0.1:8797/healthz || (echo "egress-proxy not healthy; see $(SERVE_LOG)"; exit 1)
	@echo

serve-stop:
	@if [ -f $(SERVE_PID) ]; then kill $$(cat $(SERVE_PID)) 2>/dev/null || true; rm -f $(SERVE_PID); echo "serve stopped"; fi

# Intranet API that the intranet-ro security group may reach (public audience: no bearer needed).
knowledge-facade:
	@mkdir -p $(SG_DIR)
	@if [ -f $(SG_DIR)/kf.pid ] && kill -0 $$(cat $(SG_DIR)/kf.pid) 2>/dev/null; then echo "knowledge-facade running"; else \
	  nohup $(UV) rfa knowledge-facade --host 0.0.0.0 --port 8791 --audience public > $(SG_DIR)/kf.log 2>&1 & echo $$! > $(SG_DIR)/kf.pid; \
	  echo "knowledge-facade started (pid $$(cat $(SG_DIR)/kf.pid))"; fi

knowledge-facade-stop:
	@if [ -f $(SG_DIR)/kf.pid ]; then kill $$(cat $(SG_DIR)/kf.pid) 2>/dev/null || true; rm -f $(SG_DIR)/kf.pid; fi

# ---- lifecycle ------------------------------------------------------------------------------

kb-seed:
	$(SG) kb-seed

bootstrap: validate render serve kb-seed knowledge-facade
	$(SG) bootstrap $(MCP_FLAGS)
	$(SG) status

status:
	$(SG) status

plan:
	$(SG) plan $(MCP_FLAGS)

apply:
	$(SG) apply $(MCP_FLAGS)

verify-baseline:
	$(SG) verify-baseline

teardown:
	-$(SG) teardown
	-$(MAKE) serve-stop knowledge-facade-stop

# ---- demos ----------------------------------------------------------------------------------

demo: demo-01 demo-02 demo-03 demo-04 demo-05 demo-07 demo-08 demo-09 demo-10   # 06 (admission queue) → legacy/demo

demo-01:
	$(UV) python demo/01_external_curl_blocked.py --$(DEMO_MODE)
demo-02:
	$(UV) python demo/02_security_group_change.py --$(DEMO_MODE)
demo-03:
	$(UV) python demo/03_personal_chat_self_masking.py --$(DEMO_MODE)
demo-04:
	$(UV) python demo/04_feedback_loop.py --$(DEMO_MODE)
demo-05:
	$(UV) python demo/05_injection_blocked.py --$(DEMO_MODE)
demo-07:
	$(UV) python demo/07_agents_apply_runtime_add.py --$(DEMO_MODE)
demo-08:
	$(UV) python demo/08_demote_workspace_scan.py --$(DEMO_MODE)
demo-09:
	$(UV) python demo/09_external_channel_masking.py --$(DEMO_MODE)
demo-10:
	$(UV) python demo/10_team_spawn.py --$(DEMO_MODE)
