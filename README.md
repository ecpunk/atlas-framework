# Atlas

Atlas is a system of record for the stuff you run — servers, services,
projects, and the rules for operating them — built so that both people and AI
agents work from the same facts.

Every fact lives in exactly one place, as a small YAML file with a schema
behind it. Everything you'd normally maintain by hand — the service catalog,
the project index, status reports — is generated from those files instead.
And when an AI agent wants to know what exists or change something, it goes
through the same interface with the same rules applied, instead of grepping
around and guessing.

This repo is the framework itself: the schemas, the generators, the rule
engine, the drift checker, and the MCP server. It ships with a small set of
made-up example entities (a server named `hydra`, a demo web app) so you can
run everything immediately, then swap in your own. There's no personal data
here — but the framework wasn't invented for this repo either. It was pulled
out of a real system that has been running an agent-operated homelab in
production; the examples are fictional, the patterns are not.

## Why this exists

If you run more than a handful of services, you already have this problem:
the truth about your system is scattered. Some of it is in config files, some
in a wiki page from last year, some in your head. Ask "what's actually
running, and what rules apply to it?" and you get a different answer from
every source.

That's annoying for humans. For AI agents it's dangerous. An agent with shell
access can do almost anything — what it *can't* do is know whether an action
is right for your system, or whether it's about to repeat a mistake you
already fixed once. Without a source of truth to check against, "helpful"
and "destructive" look identical.

Atlas is that source of truth. Write each fact down once, let everything else
be generated, and give agents the same governed door humans use.

## The four pieces

**The store.** Plain YAML files, one per entity — a service, a server, a
project, a rule. Schemas keep the shape right; vocabularies keep the values
right (you can't set a service's lifecycle to a made-up word). Nothing is
copy-pasted into a second location, ever.

**The generators.** Scripts that read the store and write the human-facing
views: catalogs, indexes, reports. You never edit the output. If a report is
wrong, either an entity or a generator is wrong, and you fix that instead.

**The rules.** Rules are just entities too — YAML files saying things like
"every service must name its backup plan." They're checked when entities are
validated and when views are generated. Adding a rule is a one-file change,
and it catches bad edits whether a human or an agent made them.

**The drift checker.** A scheduled job that compares what the store *says*
should be true against what's *actually* true on the machines, then sorts the
differences: fix automatically, propose to the operator, or flag for review.

```text
        people, agents, dashboards, automations
                        |
                 one shared interface
                 (Atlas MCP server)
                        |
        +---------------+---------------+
        |               |               |
     the store  -->  generators  -->  views
    (YAML files)                  (catalogs, reports)
        ^                               
        |          rules check every    
   drift checker   edit and every run   
   (says vs. is)                        
```

## Agents don't get a side door

The design rule that matters most: Atlas doesn't care who's asking. A person
at a terminal, an agent in a chat session, and a cron job all hit the same
interface, and the same rules apply to all of them.

Writes work on a propose-then-confirm basis — a change is previewed before it
lands, so an agent can't silently rewrite the record. Actions are tiered
(read-only, reversible, new-surface, irreversible), and per-consumer profiles
decide what each caller may do unattended. The validation that catches your
typo catches the agent's hallucination too.

## Try it

```bash
git clone <this repo>
cd atlas-framework
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Check the example entities against schemas, vocabularies, and rules
.venv/bin/python tools/validate.py

# See what generators exist, then preview them all against the examples
.venv/bin/python tools/pipeline.py --list
.venv/bin/python tools/pipeline.py --dry-run

# Run one generator and print what it would write
.venv/bin/python tools/pipeline.py --generator project_index --dry-run --show-content

# Start the MCP server (default: 0.0.0.0:8105/mcp)
.venv/bin/python tools/mcp_server.py
```

That validates and generates against the fictional example tree out of the
box. To make it yours, replace the files under `entities/` with your own.

A few generators can call an LLM for summarization; those are off unless you
pass `--allow-llm`, so you can't spend money by accident. `tools/pipeline.py
--help` shows the full set of flags.

## What's where

- `schemas/` — the shape each entity type must have
- `vocabularies/` — the allowed values (lifecycles, categories, tiers)
- `entities/` — the facts themselves; ships with fictional examples only
- `generators/` — store in, human-readable views out
- `extensions/` — your own generators, auto-discovered per directory
  (`extensions/hydra/` is a worked example)
- `rules/` — pipeline policy configs
- `tools/` — validate, generate, drift-check, MCP server
- `docs/` — guides for writing entities
- `outputs/` — generated views land here (gitignored)

## Configuration

Secrets come from environment variables, with a gitignored `secrets/`
directory as the file-based fallback — nothing sensitive is committed, and
`.env.example` lists what's expected. Tools that compare against external
things (systemd units, dashboards, git history) take their paths from config
and skip cleanly when those things don't exist, which is the normal state of
a fresh checkout.

## Status

Working today: the store with full validation, the generator pipeline, the
rule engine, and the MCP server with propose-confirm writes. The drift
checker runs on a schedule; richer provenance on its findings and tuning of
its escalation thresholds are still in progress.
