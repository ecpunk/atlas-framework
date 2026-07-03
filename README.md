# Atlas

> **DRAFT** — this README describes the framework accurately but the prose is a
> skeleton awaiting a final editorial pass. Structure, commands, and the worked
> example are correct; wording will be tightened.

Atlas is a canonical, agent-facing control plane for the systems you operate.
Agents, operators, automations, and dashboards read state, propose changes, and
act through a single typed surface — governed by rules, validated against
schemas, and reconciled by a drift loop you do not have to write.

This repository is the **framework**: schemas, vocabularies, the generation
pipeline, the rule engine, the drift/reconciliation tooling, and the MCP server —
plus a small set of **fictional example entities** (a `hydra` server, an
`example-web-app` service, and the `atlas` project that owns the control plane
itself) so the framework demonstrates itself out of the box. It ships with no
operator-personal data; the entity directories are yours to fill.

## The Problem Atlas Solves

Modern agent runtimes can call any tool, edit any file, and run any command.
What they cannot do is know whether the action is correct for your system,
consistent with prior decisions, or already known to be wrong. That context has
to live somewhere.

Most stacks scatter it. Some of the truth lives in configs, some in a wiki, some
in a teammate's head, some in a status page that was accurate last Tuesday. When
an agent — or a human — needs to answer "what services exist, what rules govern
them, and what is currently drifting" they get a different answer from every
source. The cost is not just confusion. It is unsafe automation, because the
executor has no ground truth to check itself against.

Atlas is where that context lives. One canonical home per fact, machine-readable,
schema-validated, and reachable through the same interface no matter who is
asking.

## How It Works

Four layers, each doing one job.

**Canonical store.** Typed YAML entities for the things you operate — projects,
services, servers, rules, agents, skills. Schemas (`schemas/`) enforce shape.
Vocabularies (`vocabularies/`) enforce values. Every fact has one home; nothing
is hand-edited into two places.

**Generation pipeline.** Generators (`generators/`) read canonical entities and
produce operator-readable views — service catalogs, project indexes, status
reports, compliance summaries. Generated views are never edited by hand. If a
view is wrong, the entity is wrong or the generator is wrong, and you fix the
cause.

**Rule engine.** Rules are entities too (`entities/rules/`). They are enforced at
validation time (schema and reference checks) and at generation time (compliance
and plan checks). Adding a rule is a YAML change, not a code change. Rules catch
drift before it ships and catch bad edits regardless of who made them.

**Drift loop.** A scheduled reconciler (`tools/drift.py`, `tools/drift_runner.py`,
`tools/remediate.py`) compares intended state against observed state, classifies
the gap, and routes it: auto-remediate for safe cleanup, propose for operator
triage, flag for review. Findings land back in the canonical store and surface
through the same interface as everything else.

## Architecture

```text
+-----------------------------------------------------------------------+
| Operators | Agents | Automations | UIs                                |
+-----------------------------------------------------------------------+
                                  |
                          same typed surface
                          same rules apply
                                  v
+-----------------------------------------------------------------------+
| Atlas MCP (read | propose | act | check)                              |
+-----------------------------------------------------------------------+
                                  |
        +-------------------------+-------------------------+
        |                         |                         |
        v                         v                         v
+-------------------+   +----------------------+   +----------------------+
| Canonical Store   |-->| Generation Pipeline  |-->| Operator Views       |
| (typed YAML)      |   | (generators)         |   | (catalogs, indexes,  |
|                   |   |                      |   | reports)             |
| projects          |   | no hand-edits to     |   +----------------------+
| services          |   | generated views      |              ^
| servers           |   +----------------------+              |
| rules             |              ^                          |
| agents            |              |                          |
| skills            |      +-------+--------+                 |
| vocabularies      |      | Rule Engine    |                 |
+---------+---------+      | validation-time|                 |
          |                | + generation-  |                 |
          |                | time checks    |                 |
          |                +----------------+                 |
          |                                                   |
          +------------> +----------------------+ ------------+
                        | Drift Loop            |
                        | observe | classify    |
                        | auto | propose | flag |
                        +----------------------+
```

## The Agnostic Surface

Atlas does not care who is asking.

A human operator running a CLI, an agent in a Claude or Copilot session, a
dashboard polling for state, an automation reconciling drift — all hit the same
typed surface with the same rules applied. The contract is the schema, not the
caller. Agents do not get a privileged shortcut, and they do not get a weakened
path. The rules that catch a careless human edit catch a confused agent edit. The
validation that protects a script protects a chat session.

This is the property that makes Atlas safe to wire into agent workflows. The
runtime executing the action is interchangeable. The system that decides whether
the action is allowed is not.

## Design Principles

- **One canonical home per fact.** State is authored once, as a typed entity.
  Every view is generated from it and never hand-edited.
- **Propose–confirm on writes.** Mutating tools preview the change first and only
  apply it on an explicit confirm, so an agent cannot silently mutate ground
  truth. Action tiers (`read_only`, `reversible`, `new_surface`, `irreversible`)
  and per-consumer profiles decide what may run unattended.
- **Rules as data.** Structural assertions are entities validated by the same
  loader they govern. Governance is a YAML change, not a code deploy.
- **Drift detection over trust.** Intended state is continuously reconciled
  against observed reality; gaps are classified and routed rather than assumed
  away.
- **Extension by convention.** Operator-specific generators live under
  `extensions/<name>/generators/` and are auto-discovered by the pipeline without
  touching core.

## Quickstart

```bash
git clone <this repo>
cd atlas-framework
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Validate the canonical entities and all cross-references
.venv/bin/python tools/validate.py

# List the generators wired up in this checkout
.venv/bin/python tools/pipeline.py --list

# Preview the full generation pipeline against the example entities
.venv/bin/python tools/pipeline.py --dry-run

# Run one generator and print its output
.venv/bin/python tools/pipeline.py --generator project_index --dry-run --show-content

# Run the MCP server (default: 0.0.0.0:8105/mcp)
.venv/bin/python tools/mcp_server.py
```

Out of the box this validates the fictional example tree (the `hydra` server, the
`example-web-app` and `example-atlas-mcp` services, the `atlas` and
`example-web-platform` projects, an example agent, consumer profile, skill, and
three framework rules) and generates catalogs, indexes, and compliance reports
from it. Replace the examples under `entities/` with your own to make it yours.

The pipeline has additional flags for running a single generator, writing outputs,
and opting into LLM-enabled generators (`--allow-llm`, gated to prevent accidental
spend). Run `tools/pipeline.py --help` for the full surface.

## Repository Layout

- `schemas/` — Pydantic schemas for entities and shared conventions
- `vocabularies/` — canonical vocabulary sets (the allowed values)
- `entities/` — canonical entity records; ships with fictional examples only
- `generators/` — core generators (store → operator views)
- `extensions/` — operator-specific generators, auto-discovered per directory
  (`extensions/hydra/` is a worked example)
- `rules/` — pipeline policy configs (automation contract, loop closure)
- `tools/` — validation, pipeline, drift/remediation, locking, MCP server
- `docs/` — entity authoring guides
- `outputs/` — generated views land here (gitignored except the placeholder)

## Configuration

- **Secrets** are read from environment variables, falling back to files under a
  gitignored `secrets/` directory (e.g. `secrets/api_key.txt`). Nothing secret is
  committed. See `.env.example` for the environment surface.
- **Output paths** default to the repo-relative `outputs/` tree. Generators and
  drift tooling that reconcile against external operator artifacts (systemd units,
  dashboards, git history) resolve those from configurable paths and degrade
  gracefully when the artifacts are absent — which is the expected state in a
  fresh checkout.

## Status

Operational today: canonical store with schema and reference validation; the
generation pipeline; the rule engine (entity and plan rule families); the MCP
server exposing read and propose/confirm write tools. The drift loop is wired and
runs; full provenance metadata on operational signals and escalation calibration
are in progress.
