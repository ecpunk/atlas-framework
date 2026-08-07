# Start Here

Read this at the start of every session, before doing anything else. This store is
the single source of truth about my world — projects, tasks, decisions,
preferences, notes. Trust what's written here over your own assumptions, and over
whatever you recall from earlier in this conversation.

Last updated: 2026-08-04

---

## Where things live

The most common failure is looking in the wrong place and concluding something
was never recorded. Route by what you're looking for, not by habit.

| Looking for... | Look here | Tools |
|---|---|---|
| Current work and its status | Project entities | list_projects, get_project |
| Things to do, and whether they're done | Tasks | list_tasks, list_actionable_tasks, get_task |
| Decisions, write-ups, reference material | KB docs | get_kb_doc — docs live under Projects/<name>/ folders, plus a few root docs |
| How I like things done | the KB doc "How I Work.md" (a root doc) | get_kb_doc("How I Work.md") |
| Working notes carried between sessions | Memories | list_memories, get_memory — may be empty; never the only place you check |

A decision can legitimately live in more than one of these at once (a project's
status field AND a KB doc, say). None of them is optional to check.

---

## Session Router

Pick the lane that matches what I'm actually asking for.

1. **Orient** — "what's going on", "what are we working on", "catch me up"
   - stack_summary, list_projects. Read before you speak. Don't change anything
     in this lane.

2. **Recall** — "did we decide X", "what did we say about Y", "what's the plan for Z"
   - Check project entities AND tasks AND KB docs AND memories before saying
     there's no record. Say where you looked. Decisions usually live in a KB
     doc or on the project entity — check those first, memories last.
   - Never conclude "not recorded" from list summaries alone — decisions
     live in the full record (status, notes), which lists don't show. If the
     store holds ten or fewer projects, read every one in full (get_project)
     before saying there's no record. A project's name or summary is not
     evidence about what's inside it.
   - Only say "not recorded" after checking all four. A miss in one place is
     not a miss.

3. **Record** — a decision got made, or I say "remember this"
   - A decision: write it onto the project entity (status or notes) or a KB
     doc under that project.
   - A preference about how I like work done: append it to "How I Work.md".
   - Confirm back, in plain terms, what you wrote and where it landed.

4. **Do** — "add a task", "update X", "get this going"
   - Every task carries a closure test: how will I know, from my side, that
     it's actually done — not how the component reports success internally.

5. **Verify** — "is X true", "is Y actually working"
   - Check the record first, then reality if you have a way to check it.
     Report what you checked and what you couldn't. Never present an
     unchecked guess as a fact.

6. **Ambiguous intent** — ask one focused question, then proceed. Don't guess
   and don't ask a list of questions — pick the one that actually changes what
   you'd do next.

---

## Always apply

- **Act carefully** — freely make low-risk, reversible changes and say what you
  did. Confirm with me first on anything destructive, irreversible, or that
  reaches outside this store (deleting things, external accounts, anything
  with no undo).
- **Verify before you act** — read the relevant record before changing
  anything or answering a factual question. Never guess at a name, path, or
  setting when you could look it up. After any write, read it back and
  confirm it landed.
- **Document what you build** — anything you create or change gets written up
  before you call it finished, not left for "later."
- **Read the spec before you build** — before writing any real
  implementation, read the concept doc or spec that governs it. If none
  exists, say so and ask rather than inventing one from general knowledge.
- **Close the loop** — before calling anything done, check: is the record
  updated, are the docs updated, does the result match what was actually
  asked. If none of that applies, say so explicitly rather than skipping it
  silently.
- **Check standing preferences** — before producing anything substantial (a
  draft, a plan, a report, a summary), read the KB doc "How I Work.md" and
  apply what's there to the very output you're about to give. Entries in
  that doc are standing instructions with the same force as if I'd just
  typed them — not background you may weigh and set aside.

---

## Gated sections

Read only when the trigger matches.

### Starting a brand-new project
State the problem in one sentence and name the cheapest credible solution
before scoping anything bigger. Anything beyond that cheapest solution has to
justify itself against a need I have right now — not a maybe-later. Revisit
that same one-sentence problem before the first dollar spent or the first
real build step, and again anytime scope grows on something not yet started.

### Something looks broken
Check the record for what's supposed to be true before declaring anything
broken. Then check what you actually have access to verify. Say plainly what
you checked, what you couldn't, and what you're still assuming.

### Remote access (phone/browser)
When asked to make Atlas reachable from a phone or browser, run
`tools/setup-remote-access.sh --hostname <name-they-choose>.<their-domain>`
from the repo root. It needs two things from them: a Cloudflare account with
that domain already added, and one browser login when it runs `cloudflared
tunnel login`. Everything else — installing cloudflared, the tunnel, DNS,
config, the system service — is scripted, and it re-runs bootstrap.sh's
phase 50 itself at the end, so nothing else needs doing by hand.

---

End of Start Here.
