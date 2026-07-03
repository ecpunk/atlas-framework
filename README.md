# Atlas

Atlas is the coordination layer for an environment operated by both humans
and AI agents: a single, schema-validated record of what exists, what work is
in flight, and what each actor is permitted to do — with every human-facing
view generated from it rather than maintained by hand.

Think of it as the control tower at a small airfield. The pilots — some
human, some autonomous — never negotiate with each other directly. Every one
of them flies against the tower's picture of the field, requests clearance
before acting, and how much each may do without asking depends on who they
are and how risky the maneuver is. Everything that moves is logged. Atlas is
that tower, for an environment where AI agents do real work.

This repository is the framework: the schemas, the vocabularies, the
generation pipeline, the rule engine, the drift tooling, and the MCP server
that agents connect to. It ships with a small set of fictional example
entities (a server named `hydra`, a demo web application) so everything runs
out of the box, and contains no operator-personal data. The framework itself
was not invented for this repository — it was extracted from a production
system that governs a real, agent-operated environment every day. The
examples are fictional; the patterns are not.

## Why this exists

An AI agent with shell access can do almost anything. What it cannot do, on
its own, is know whether an action is correct for your environment,
consistent with decisions already made, or something you explicitly ruled out
last month. That knowledge has to live somewhere, and in most environments it
is scattered — some in config files, some in a wiki, some in the operator's
head. Every consumer gets a different answer, and an agent acting on the
wrong one is indistinguishable from a helpful agent right up until it isn't.

Atlas gives that knowledge one home. In production use it earns its keep in
three distinct roles, and the framework in this repository implements all
three.

**Shared memory.** The store records not just infrastructure — servers,
services, projects — but work: tasks and sessions are entities too. Agents
log what they did, pick up what's open, and build on each other's context
instead of rediscovering it. In practice this is the highest-traffic part of
the system: the store functions as the ledger through which humans and agents
coordinate.

**Permission system.** Consumer profiles declare what each actor — the
operator at a terminal, an attended chat agent, an unattended automation — is
entitled to do, expressed against a shared vocabulary of action tiers
(`read_only`, `reversible`, `new_surface`, `irreversible`). Every surface
asks the same question and gets the same verdict: allow, confirm, or deny.
Rules are entities as well, so tightening policy is a YAML change, not a
deploy.

**View generator.** Everything a human reads — service catalogs, project
indexes, dashboards, status rollups — is generated from the store by a
pipeline of small generators. No view is ever edited by hand. If a report is
wrong, an entity or a generator is wrong, and you fix the cause.

A fourth piece keeps the record honest: a scheduled **drift checker**
compares what the store says should be true against what is actually true on
the machines, classifies each gap, and routes it — remediate automatically,
propose to the operator, or flag for review.

## How it's built

Entities are plain YAML files, one per fact, validated against Pydantic
schemas and canonical vocabularies — a service cannot declare a lifecycle
value that doesn't exist. Generators read the store and emit views;
extensions add operator-specific generators without touching core. Rules are
checked at validation time and again at generation time. The MCP server
exposes the whole surface — reads, proposals, checks — to any MCP-capable
agent runtime.

Writes follow a propose-then-confirm contract: a mutation is previewed before
it is applied, so an agent cannot silently rewrite the record. The same
schemas, the same rules, and the same confirmation gates apply to every
caller. There is no privileged path for humans and no weakened one for
agents — which is precisely what makes it safe to let agents in at all.

## Try it

```bash
git clone <this repo>
cd atlas-framework
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Validate the example entities against schemas, vocabularies, and rules
.venv/bin/python tools/validate.py

# List the generators, then preview the full pipeline against the examples
.venv/bin/python tools/pipeline.py --list
.venv/bin/python tools/pipeline.py --dry-run

# Run one generator and print what it would write
.venv/bin/python tools/pipeline.py --generator project_index --dry-run --show-content

# Start the MCP server (default: 0.0.0.0:8105/mcp)
.venv/bin/python tools/mcp_server.py
```

This validates and generates against the fictional example tree out of the
box. To make it yours, replace the entities under `entities/` with your own.

A few generators can call an LLM for summarization; these are disabled unless
you pass `--allow-llm`, so nothing spends money by accident. Run
`tools/pipeline.py --help` for the full flag surface.

## Repository layout

- `schemas/` — the shape each entity type must have
- `vocabularies/` — the allowed values (lifecycles, categories, action tiers)
- `entities/` — the canonical records; ships with fictional examples only
- `generators/` — store in, human-readable views out
- `extensions/` — operator-specific generators, auto-discovered per directory
  (`extensions/hydra/` is a worked example)
- `rules/` — pipeline policy configuration
- `tools/` — validation, pipeline, drift and remediation, MCP server
- `docs/` — entity authoring guides
- `outputs/` — generated views land here (gitignored)

## Configuration

Secrets are read from environment variables, with a gitignored `secrets/`
directory as the file-based fallback; nothing sensitive is committed, and
`.env.example` documents what is expected. Tools that reconcile against
external artifacts (systemd units, dashboards, git history) resolve those
from configurable paths and skip cleanly when they are absent — the normal
state of a fresh checkout.

## Status

Working today: the canonical store with full schema, vocabulary, and
reference validation; the generation pipeline; the rule engine; consumer
profiles and action tiers; and the MCP server with propose-confirm writes.
The drift checker runs on a schedule; richer provenance on its findings and
tuning of its escalation thresholds are in progress.
