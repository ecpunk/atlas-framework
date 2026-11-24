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
everything you record is committed to YOUR local clone, and Atlas never
pushes it anywhere.

Every write to the knowledge base goes through the server, which stamps the
doc's frontmatter with who wrote it and when (`generated: {by, at}`, from
the [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)).
A write must name the agent or process making it; a person is never
recorded as the author, and empty or placeholder content is refused.

For what Atlas does in day-to-day use and what each part gets you, see
[How we run Atlas](docs/how-we-run-atlas.md).

## Quick start (fresh Ubuntu box)

Run these in the SSH window on your own computer (see the install guide's
"Switch to your own computer" section) — not at the machine's own keyboard.

First this line by itself (it asks for your password):

```
sudo mkdir -p /opt/stack && sudo chown "$USER" /opt/stack
```

Then these three lines as one paste:

```
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

## Local install (Windows, macOS or Linux — no server)

For one person on their own computer, Atlas can run as a local MCP server
that Claude Code starts by itself: no network port, no login secret, no
system service. You need Python 3.10+, git, and Claude Code.

```
git clone https://github.com/ecpunk/atlas-framework.git atlas
cd atlas
python install_local.py
```

(On macOS/Linux the command may be `python3`.) The installer builds a private
`.venv/` inside the clone, seeds the starter rules, runs a self-test, and
registers the server with Claude Code for your user. Start a new Claude Code
session and Atlas is available in every project.

Your records stay in the clone: every entity and KB doc is committed to the
clone's own git history, and the installer disables `git push` for the clone
so that history cannot be sent anywhere. `git pull` still brings engine
updates. Run `python install_local.py --help` for the options (for example
`--exclude ""` to keep every tool).

## Layout

- `bootstrap.sh` + `lib/` — the server install: provisioning phases (each
  runnable standalone).
- `install_local.py` — the local install for Windows, macOS or Linux (stdio,
  no service).
- `tools/` — the MCP server (`mcp_server.py`) and its supporting modules
  (entity store loader, reference/link checker, file-lock helper, OAuth
  provider, KB frontmatter stamper, validator, session-retention sweep,
  service health prober), plus `local_server.py` (the stdio entry point
  `install_local.py` registers) and `setup-remote-access.sh` (the
  follow-on step for reaching a server install from outside).
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
- `mcp-instructions.md` — routing notes appended to the instructions the
  server gives connecting agents.
- `fixtures/claude/CLAUDE.md` — installed to `~/.claude/CLAUDE.md` by
  bootstrap so Claude Code sessions on this machine orient from the store.
- `systemd/` — unit templates rendered and installed by bootstrap.
- `recovery/` — plain-language recovery steps + the login-secret reset script.
- `docs/index.html` — the step-by-step setup guide (served by GitHub Pages);
  `docs/how-we-run-atlas.md` — how the full deployment runs.
- `VERSION` — which upstream engine build this came from, and when.
- `LICENSE` — MIT.

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

## License

MIT. See `LICENSE`.
