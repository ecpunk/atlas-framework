"""
infra_map — Atlas pipeline generator

Reads all service entities and produces apps/dashboard/infra-map.json,
which infra.html fetches to render the infrastructure topology view.

Output shape:
{
  "generated_at": "2026-05-18T...",
  "entries": [
    {
      "id": "home-command",
      "name": "Home Command",
      "group": "Apps",
      "domain": "home.example.com",
      "port": 8094,
      "auth_gated": true,
      "ssl": true,
      "portal_proxied": false,
      "notes": null
    }
  ]
}

Inclusion rules:
  - lifecycle in {running, maintained}
  - has at least one of: external_url, portal_path, portal_url

Special case — example-club:
  Has both a public external_url (example-club.com, ungated) and an
  auth-gated admin URL (example-club.example.com). Generator emits two entries.

Grouping: maps tier → group label (same as portal_nav).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from schemas.service import Service

NAME = "infra_map"
INPUTS = ["service:*"]
OUTPUTS = [
    "outputs/dashboard/infra-map.json"
]

RUNNING = "vocab:service_lifecycles:running"
MAINTAINED = "vocab:service_lifecycles:maintained"
ACTIVE_LIFECYCLES = {RUNNING, MAINTAINED}

# MCP/agent tier — surfaced as its own group regardless of public exposure.
MCP_TYPE = "vocab:service_types:mcp_http"
MCP_GROUP = "MCP / Agents"


def _is_mcp(svc: Service) -> bool:
    return str(svc.service_type) == MCP_TYPE


def _public_url(svc: Service) -> str | None:
    """A service's public edge: explicit external_url, or a public https
    health_endpoint (MCPs record their public ingress there; internal ones
    use http://127.0.0.1:port)."""
    if svc.external_url:
        return svc.external_url
    he = svc.health_endpoint or ""
    if he.startswith("https://") and "127.0.0.1" not in he and "localhost" not in he:
        return he
    return None

TIER_LABELS = {
    "ops": "Ops & Auth",
    "media": "Media",
    "games": "Games",
    "apps": "Apps",
}

# Services that always appear at the top regardless of tier
OPS_FIRST = {"ops-dashboard", "auth-gate", "host-nginx-proxy"}


def _domain_from_url(url: str) -> str:
    """Strip scheme from a URL to get the domain+path portion."""
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            return url[len(scheme):]
    return url


def _entry(svc: Service, domain: str, auth_gated: bool, ssl: bool,
           portal_proxied: bool, notes: str | None = None) -> dict[str, Any]:
    tier = (svc.tier or "other").lower()
    group = TIER_LABELS.get(tier, tier.title() if tier else "Other")
    entry: dict[str, Any] = {
        "id": svc.id,
        "name": svc.portal_label or svc.name,
        "group": group,
        "domain": domain,
        "port": svc.port,
        "auth_gated": auth_gated,
        "ssl": ssl,
        "portal_proxied": portal_proxied,
    }
    if notes:
        entry["notes"] = notes
    return entry


def generate(store: dict) -> dict[str, str]:
    import json

    services_store = store.get("service", {})
    services: list[Service] = [
        s for s in services_store.values()
        if isinstance(s, Service)
    ]

    # Filter to active services with some network exposure
    def has_exposure(svc: Service) -> bool:
        return bool(svc.external_url or svc.portal_path or svc.portal_url)

    active = [
        s for s in services
        if str(s.lifecycle) in ACTIVE_LIFECYCLES and has_exposure(s)
    ]

    entries: list[dict[str, Any]] = []

    for svc in active:
        if _is_mcp(svc):
            continue  # MCP/agent tier rendered in its own pass below
        auth_gated = svc.auth_gated or False

        # Special case: example-club has both a public domain and an
        # auth-gated example.com admin URL — emit two rows.
        if (svc.external_url and svc.portal_url
                and "example.com" not in svc.external_url
                and "example.com" in svc.portal_url):
            # Public entry (ungated)
            public_domain = _domain_from_url(svc.external_url)
            ssl_public = svc.external_url.startswith("https://")
            entries.append(_entry(
                svc, public_domain, auth_gated=False, ssl=ssl_public,
                portal_proxied=False,
                notes=f"{svc.summary} — public site",
            ))
            # Admin entry (gated via example.com)
            admin_domain = _domain_from_url(svc.portal_url)
            ssl_admin = svc.portal_url.startswith("https://")
            admin_entry = _entry(
                svc, admin_domain, auth_gated=True, ssl=ssl_admin,
                portal_proxied=False,
                notes="Internal admin access via auth-gate",
            )
            admin_entry["name"] = f"{svc.portal_label or svc.name} (admin)"
            entries.append(admin_entry)
            continue

        if svc.portal_path:
            # Portal-proxied service — no standalone subdomain
            domain = f"portal.example.com{svc.portal_path}"
            ssl = True
            portal_proxied = True
            # Portal itself is auth-gated, so portal-proxied services are too
            effective_auth = True
        elif svc.external_url:
            domain = _domain_from_url(svc.external_url)
            ssl = svc.external_url.startswith("https://")
            portal_proxied = False
            effective_auth = auth_gated
        elif svc.portal_url:
            domain = _domain_from_url(svc.portal_url)
            ssl = svc.portal_url.startswith("https://")
            portal_proxied = False
            effective_auth = auth_gated
        else:
            continue

        entries.append(_entry(
            svc, domain, auth_gated=effective_auth, ssl=ssl,
            portal_proxied=portal_proxied,
            notes=svc.summary if len(svc.summary) < 120 else None,
        ))

    # MCP / agent tier pass — include every running/maintained mcp_http service,
    # whether or not it has a public edge. Internal-only MCPs show their LAN
    # endpoint; exposed MCPs (e.g. atlas-mcp) show their public domain.
    mcp_services = [
        s for s in services
        if str(s.lifecycle) in ACTIVE_LIFECYCLES and _is_mcp(s)
    ]
    for svc in mcp_services:
        pub = _public_url(svc)
        exposed = bool(pub)
        if exposed:
            domain = _domain_from_url(pub).split("/")[0]  # host only, drop path
            ssl = pub.startswith("https://")
        else:
            domain = f"127.0.0.1:{svc.port}" if svc.port else "internal"
            ssl = False
        auth_kind = (svc.auth.mechanism if getattr(svc, "auth", None) else None) or "none"
        entries.append({
            "id": svc.id,
            "name": svc.portal_label or svc.name,
            "group": MCP_GROUP,
            "domain": domain,
            "port": svc.port,
            "auth_gated": False,
            "ssl": ssl,
            "portal_proxied": False,
            "internal": not exposed,
            "auth_kind": auth_kind,
        })

    # Sort: ops-first services first, then by group label, then by name
    def sort_key(e: dict) -> tuple:
        priority = 0 if e["id"] in OPS_FIRST else 1
        return (priority, e["group"], e["name"])

    entries.sort(key=sort_key)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": NAME,
        "note": "AUTO-GENERATED by Atlas infra_map generator. Do not hand-edit.",
        "entries": entries,
    }

    return {
        OUTPUTS[0]: json.dumps(output, indent=2) + "\n",
    }
