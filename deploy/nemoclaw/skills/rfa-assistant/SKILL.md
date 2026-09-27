---
name: rfa-assistant
description: Ask the owner's RFA personal assistant (knowledge base, Task teams, drafts) through its authenticated channel. Use for any request to remember, find, summarize, investigate or verify the owner's own notes and projects.
---

# RFA assistant channel

You are running inside a NemoClaw sandbox. The owner's RFA assistant runs on the host and is
reachable ONLY at the endpoint in `/sandbox/rfa-channel.env` (variable `RFA_CHANNEL_URL`),
with the bearer header stored in `/sandbox/rfa-auth.hdr`. Never print, echo or copy the header
file contents. Never try other hosts, ports or paths: the OpenShell policy denies them.

## How to answer a user request

1. Run exactly one request per user message (retry once only on a network error):

   ```sh
   . /sandbox/rfa-channel.env
   printf '%s' "$RFA_REQUEST_JSON" > /tmp/rfa-req.json
   curl -s -m 170 -H @/sandbox/rfa-auth.hdr -H 'Content-Type: application/json' \
        -X POST "$RFA_CHANNEL_URL/channel/chat" --data-binary @/tmp/rfa-req.json
   ```

   where `RFA_REQUEST_JSON` is `{"text": "<the user's message verbatim>"}`. If the user
   continues the same conversation, add `"session_id": "<session_id from the previous reply>"`.

2. Read the JSON reply. Report to the user:
   - `reply` verbatim (this is the assistant's answer; do not paraphrase or invent details),
   - one short line with `route.kind`/`route.label` (who handled it: Task team, storage space, or the assistant),
   - the `stages` list in order (the assistant's processing states),
   - `answer_model` or `team` when present (which model or Task team produced it; keep "simulated" flags).

3. If the HTTP status is not 201, report the status and the `code` field and stop. Do not retry
   with different paths, do not read or modify files outside `/tmp`, and do not attempt any
   other network access.

The assistant, not you, decides whether a note is stored, which Task team answers, or whether a
public draft is created. You only relay the owner's message and the assistant's reply.
