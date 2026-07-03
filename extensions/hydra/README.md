# Hydra Extension

Substrate-specific Atlas extension for the Hydra home server stack.

## Scope
Provides generators, rules, and (future) detectors for resources hosted on Hydra: systemd units, nginx vhosts, Docker Compose projects, the platform service catalog, and the knowledge-server MCP.

## Contract
Extensions are **additive only**. Hydra rules and generators add to core; they never override core behavior. If Hydra needs different behavior than core, it does so by adding a more specific rule, not by suppressing a core rule.

## What this extension owns
- `generators/kb_buckets.py` — knowledge-server VALID_STAGES
- `generators/service_catalog.py` — platform/service-catalog.json
- `generators/servers_index.py` — Hydra server inventory

## Future
- `rules/` — Hydra-specific rules (port assignments, auth-gate patterns, FAILURE MODES requirements)
- `detectors/` — drift detectors that walk Hydra paths
- `probes/` — reality probes for systemd units, docker compose, nginx
