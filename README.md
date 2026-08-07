# Atlas

Atlas is a canonical entity store: a small MCP (Model Context Protocol) server
backed by plain YAML files under version control, plus a schema layer that
validates every entity before it is written. It exists so an AI agent (or a
person) always has one place to ask "what is true right now" — services,
projects, tasks, rules, sessions — instead of that information scattering
across chat logs, ad-hoc docs, and memory.

Cloning this repo gives you a complete, independent instance: the engine
(`tools/`, `schemas/`, `vocabularies/`), an empty entity store, a small seed
set of agent-behavior rules, and a bootstrap script that stands the whole
thing up as a systemd service on an Ubuntu machine. Your data stays yours:
everything you record is committed to YOUR local clone and never pushed
anywhere.

## Quick start (fresh Ubuntu box)

Run these in the SSH window on your own computer (see the install guide's
"Switch to your own computer" section) — not at the machine's own keyboard.
Copy and paste them one at a time:

```
sudo mkdir -p /opt/stack && sudo chown "$USER" /opt/stack
git clone https://github.com/ecpunk/atlas-framework.git /opt/stack/atlas-store
cd /opt/stack/atlas-store
./bootstrap.sh
```

`bootstrap.sh` checks prerequisites, builds a private virtualenv, seeds the
store, generates a login secret (printed exactly once — store it), installs
and starts the systemd units, and health-checks the result. On a brand-new
install, the first run may pause for up to half an hour while the system
finishes its own first-boot updates — it says so on screen when that
happens; just let it sit. It is idempotent:
re-run it after any failure and it picks up where it stopped. `./bootstrap.sh
--help` lists the options. When it finishes, it prints the next steps
(installing Claude Code, connecting from your phone/browser).

## Layout

- `bootstrap.sh` + `lib/` — the provisioning phases (each runnable standalone).
- `tools/` — the MCP server (`mcp_server.py`) and its supporting modules
  (entity store loader, reference/link checker, file-lock helper, OAuth
  provider, validator, session-retention sweep, service health prober).
- `schemas/` — pydantic models, one per entity type. These define what a
  valid entity looks like and are the enforcement layer behind every write.
- `vocabularies/` — controlled value lists (statuses, severities, lifecycle
  stages, etc.) that entity fields reference instead of free-text strings.
- `entities/` — the actual data, one YAML file per entity, grouped by type.
  Starts empty; fills with your own records as you use it.
- `docs/kb/` — the instance's knowledge base. Ships with `Start Here.md`
  (how an agent should orient in this store) and `How I Work.md` (your
  standing preferences — agents append to it as you state them).
- `seed/rules/` — the curated starter rules `bootstrap.sh` copies into
  `entities/rules/` exactly once.
- `fixtures/claude/CLAUDE.md` — installed to `~/.claude/CLAUDE.md` by
  bootstrap so Claude Code sessions on this machine orient from the store.
- `systemd/` — unit templates rendered and installed by bootstrap.
- `recovery/` — plain-language recovery steps + the login-secret reset script.
- `VERSION` — which upstream engine build this came from, and when.

## Running it by hand

The systemd unit does this for you, but the server is just:

```
.venv/bin/python tools/mcp_server.py
```

Validate the store at any time with:

```
.venv/bin/python tools/validate.py
```

## Upgrading

```
cd /opt/stack/atlas-store
git pull --no-edit
./bootstrap.sh
sudo systemctl restart atlas-mcp
```

Your entities and KB edits live in local commits, so a pull merges engine
updates around them. If `git pull` reports a conflict (you and an engine
update both changed the same file, usually a `docs/kb/` doc), that is git
protecting your edit — ask your AI assistant to resolve the merge, or keep
your version with `git checkout --ours <file> && git add <file> && git
commit`.

## Provenance

This repo is assembled from a portable subset of a larger private Atlas
deployment's engine code. Entity data is never included — the store starts
empty on every instance. See `VERSION` for the exact source build.
