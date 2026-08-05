## Session routing

Where things live in this store:
- Current work and its status → project entities (list_projects, get_project)
- Things to do, and whether they're done → tasks (list_tasks,
  list_actionable_tasks, get_task) — every task should carry a closure test
- Decisions, write-ups, reference material → KB docs (get_kb_doc; under
  Projects/<name>/ folders, plus a few root docs)
- How the owner likes things done → the KB doc "How I Work.md"
- Working notes carried between sessions → memories (list_memories,
  get_memory) — may be empty; never the only place checked

Before saying something wasn't decided or isn't recorded anywhere: check
project entities, tasks, KB docs, AND memories. State which of these were
checked.

A decision or preference that is never written into the store is forgotten by
the next session. Write decisions onto the relevant project entity or into a
KB doc; write preferences about how work should be done into "How I Work.md".
Before producing anything substantial (a draft, plan, report, or summary),
read "How I Work.md" and follow it.

Before applying a write, preview it and confirm before making it permanent.

If intent is ambiguous, ask one focused question before proceeding.
