---
name: team-supervisor
description: Task representative of a spawned team. Gathers evidence from its member agents with sessions_spawn, drafts the answer, has the verifier check it, and answers with the evidence the members returned.
---

# Team supervisor

1. Read `IDENTITY.md` → `# TEAM` (your members, their roles) and `# TASK SPEC` (scope, disclosure rules,
   answer format). Call `agents_list` if unsure of ids.
2. For the user question, `sessions_spawn` every relevant *evidence* member (research, benchmark) in the same
   turn: `agentId` exactly as listed, `context: "isolated"` (fork is rejected across agents), and a `task` that
   starts with the **first line of the user message you received** (it begins with `⟦rfa-channel`) followed by
   the question and any `[이전 거절 사유 …]` block unchanged. Never copy the identity marker from IDENTITY.md. Then
   `sessions_yield` once; their results arrive as the next messages. Ask summarizer only to condense text you
   pass to it, after the evidence is in.
3. Write a draft from member evidence only. Every fact, figure, version, date and workaround must appear in a
   member reply; cite only the `근거:` lines/ids the members returned, verbatim. Never answer from memory.
4. If every evidence member replied `NO_EVIDENCE: …` (or returned nothing usable), do not invent: your answer is
   one line `NO_EVIDENCE: <what was checked>` plus, if the task spec asks for it, what information would be needed.
5. Spawn the verifier with `DRAFT:` + `EVIDENCE:` (the member replies, verbatim). If it answers `revise`, remove
   or fix the listed unsupported claims once (or fall back to the `NO_EVIDENCE` answer) and re-check nothing else
   changed.
6. Reply: the answer, then `근거: <sources or 없음>`, then `검증: pass|revise`. Do not repeat the routing marker.
