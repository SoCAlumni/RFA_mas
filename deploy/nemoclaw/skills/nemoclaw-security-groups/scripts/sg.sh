#!/bin/sh
# Wrapper: run the security-group controller from the repository root.
set -eu
ROOT=${RFA_MAS_ROOT:-$(cd "$(dirname "$0")/../../../../.." && pwd)}
export PATH="$HOME/.hermes/node/bin:$HOME/.local/bin:$PATH"
cd "$ROOT"
exec uv run --offline --frozen python -m rfa_mas.nemoclaw "$@"
