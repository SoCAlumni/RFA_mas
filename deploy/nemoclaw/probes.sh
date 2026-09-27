#!/bin/sh
# P1-007A sandbox probes (run inside the NemoClaw sandbox by tests/integration/test_nemoclaw_live.py).
# __BASE__ and __OTHER__ are substituted by the test; results are one JSON line per probe.
set -u
H=/sandbox/rfa-auth.hdr
B=__BASE__
rec() { printf '{"probe":"%s","http":"%s","curl_exit":%s,"body":%s}\n' "$1" "$2" "$3" "$4"; }
body() { tr -d '\n' < "$1" | head -c 400 | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }
q() { o="$1"; shift; curl -s -m 60 -o "$o" -w '%{http_code}' -H @"$H" -H 'Content-Type: application/json' "$@"; }
code=$(curl -s -m 20 -o /tmp/h -w '%{http_code}' "$B/healthz"); rec healthz "$code" $? "$(body /tmp/h)"
code=$(q /tmp/s -X POST "$B/v1/sessions" -d '{}'); rec session_create "$code" $? "$(body /tmp/s)"
sid=$(sed -n 's/.*"session_id":"\([^"]*\)".*/\1/p' /tmp/s | head -1)
code=$(q /tmp/w1 -X POST "$B/v1/sessions/$sid/work" -d '{"query":"TRIV3 공개 트랙을 근거와 함께 요약해 줘.","domain_id":"triv3","target":{"audience":"owner"}}'); rec work_owner "$code" $? "$(body /tmp/w1)"
code=$(q /tmp/w2 -X POST "$B/v1/sessions/$sid/work" -d '{"query":"TRIV3 공개 트랙을 근거와 함께 요약해 줘.","domain_id":"triv3","target":{"audience":"public"}}'); rec work_public "$code" $? "$(body /tmp/w2)"
run=$(sed -n 's/.*"run_id":"\([^"]*\)".*/\1/p' /tmp/w1 | head -1)
code=$(q /tmp/r "$B/v1/runs/$run"); rec run_get "$code" $? "$(body /tmp/r)"
code=$(q /tmp/d1 -X POST "$B/v1/knowledge/sources" -d '{}'); rec knowledge_write "$code" $? "$(body /tmp/d1)"
code=$(q /tmp/d2 "$B/v1/sessions"); rec sessions_list "$code" $? "$(body /tmp/d2)"
code=$(q /tmp/d3 -X DELETE "$B/v1/knowledge/sources/x"); rec knowledge_delete "$code" $? "$(body /tmp/d3)"
code=$(q /tmp/d4 -X POST "$B/v1/candidates/x/decision" -d '{}'); rec candidate_decision "$code" $? "$(body /tmp/d4)"
code=$(q /tmp/d5 "$B/v1/knowledge/derived"); rec knowledge_derived "$code" $? "$(body /tmp/d5)"
code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' https://example.com/); rec example_com "$code" $? '""'
code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "__OTHER__"); rec other_port "$code" $? '""'
