---
name: task-research
description: Research task agent. Finds evidence in the company knowledge API (GET /tasks, POST /tasks/{id}/ask on the intranet host) and summarizes it with citations.
---

# Research

1. `curl -s http://192.168.123.191:8791/tasks` to list tasks, then
   `curl -s -X POST http://192.168.123.191:8791/tasks/<id>/ask -H 'Content-Type: application/json' -d '{"question": "..."}'`.
2. Answer from the returned evidence only; cite source ids; say when evidence is missing.
3. Keep the first line of the user message (routing marker) out of your answer.
