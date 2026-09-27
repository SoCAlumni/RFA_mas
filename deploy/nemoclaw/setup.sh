#!/bin/sh
# P1-008M: expose the running PoC to the NemoClaw sandbox agent.
#   deploy/nemoclaw/setup.sh <sandbox> <host-lan-ip> <port> <data-dir>
# Applies the rfa-assistant OpenShell preset (dry-run first), uploads the bearer header file
# (0600, never echoed) and the channel env, and installs the rfa-assistant skill.
set -eu
SB=${1:?sandbox}; HOST=${2:?host ip}; PORT=${3:?port}; DATA=${4:?poc data dir}
HERE=$(cd "$(dirname "$0")" && pwd)
KEYFILE="$DATA/channel-api.key"
[ -s "$KEYFILE" ] || { echo "channel key missing: start the PoC with --channel-bind first" >&2; exit 2; }
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
sed "s/__HOST__/$HOST/; s/__PORT__/$PORT/" "$HERE/rfa-assistant.yaml" > "$TMP/rfa-assistant.yaml"
umask 077
printf 'Authorization: Bearer %s\n' "$(cat "$KEYFILE")" > "$TMP/rfa-auth.hdr"
printf 'RFA_CHANNEL_URL=http://%s:%s\n' "$HOST" "$PORT" > "$TMP/rfa-channel.env"
nemoclaw "$SB" policy add --from-file "$TMP/rfa-assistant.yaml" --trusted-private-host "$HOST" --dry-run
nemoclaw "$SB" policy add --from-file "$TMP/rfa-assistant.yaml" --trusted-private-host "$HOST" --yes
nemoclaw "$SB" upload "$TMP/rfa-auth.hdr" /sandbox/rfa-auth.hdr
nemoclaw "$SB" upload "$TMP/rfa-channel.env" /sandbox/rfa-channel.env
nemoclaw "$SB" exec --timeout 30 -- chmod 600 /sandbox/rfa-auth.hdr
nemoclaw "$SB" skill install "$HERE/skills/rfa-assistant"
echo "rfa-assistant channel ready for sandbox $SB -> http://$HOST:$PORT"
