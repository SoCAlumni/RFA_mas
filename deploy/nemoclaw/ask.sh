#!/bin/sh
# P1-008M: one owner turn through the NemoClaw sandbox agent (OpenClaw + rfa-assistant skill).
#   deploy/nemoclaw/ask.sh <sandbox> "<message>" [session-id]
set -eu
SB=${1:?sandbox}; MSG=${2:?message}; SID=${3:-}
PROMPT="Use the rfa-assistant skill. Send this owner message to the RFA assistant channel"
[ -n "$SID" ] && PROMPT="$PROMPT (continue session_id $SID)"
PROMPT="$PROMPT and report the reply, route, stages and model/team exactly as the skill says. Message: $MSG"
exec nemoclaw "$SB" agent --session-id "rfa-owner" --thinking off -m "$PROMPT"
