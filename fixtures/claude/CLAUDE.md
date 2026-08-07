# Global rules

Loaded in every session, in this project and every other one.

**First action, every session:** call `get_kb_doc("Start Here.md")` on the
**local `atlas` MCP server** — the one this machine runs itself, configured
in `/opt/stack/atlas-store/.mcp.json` — and follow it. Never use an
account-level or cloud "Atlas" connector for this store's reads or writes,
even if one appears in the tool list; those belong to a different store.
If the local atlas MCP isn't reachable, read the file
`/opt/stack/atlas-store/docs/kb/Start Here.md` directly, follow it, and
mention that the MCP was unreachable.

**Before telling me something wasn't decided or isn't recorded:** check
projects, tasks, KB docs, AND memories — not just one of them. A miss in one
place is not a miss.

**Record it or lose it.** Write decisions into the store — onto the relevant
project or into a KB doc — not just into this chat. Write preferences about
how I like things done into the KB doc "How I Work.md". A recording isn't
done until the write is confirmed and read back. Anything that stays only in
conversation is forgotten by the next session.

**Before producing anything substantial** — a draft, a plan, a report, a
summary — read `get_kb_doc("How I Work.md")` and follow it.
