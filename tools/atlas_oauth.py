"""Atlas self-hosted OAuth 2.1 Authorization Server provider.

Implements mcp.server.auth's OAuthAuthorizationServerProvider for the AtlasMCP
server so atlas-mcp is its OWN authorization server — no third-party vendor, no
Cloudflare in the auth path. Single-operator design:

- Dynamic Client Registration (DCR) so Claude clients (claude.ai / Code / Desktop)
  self-register. Stored persistently.
- Opaque access/refresh tokens persisted in SQLite (survive restarts; no JWKS needed —
  the resource server validates by calling load_access_token()).
- A single allowed principal; login at /oauth/login is gated by a shared secret
  (ATLAS_OAUTH_LOGIN_SECRET_FILE). PKCE/S256 is enforced by the SDK token handler.

This module is import-safe and has NO effect until mcp_server.py wires it behind the
ATLAS_OAUTH_ENABLED flag (P5 cutover). Building/committing it changes nothing at runtime.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

# ---------------------------------------------------------------------------
# Config (env-driven; all optional with sane defaults)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("ATLAS_OAUTH_DB", str(REPO_ROOT / ".secrets" / "oauth.sqlite3")))
ISSUER_URL = os.environ.get("ATLAS_OAUTH_ISSUER", "https://atlas-mcp.example.com").rstrip("/")
RESOURCE_URL = os.environ.get("ATLAS_OAUTH_RESOURCE", ISSUER_URL).rstrip("/")
LOGIN_PATH = os.environ.get("ATLAS_OAUTH_LOGIN_PATH", "/oauth/login")
PRINCIPAL = os.environ.get("ATLAS_OAUTH_PRINCIPAL", "jon")
ACCESS_TTL = int(os.environ.get("ATLAS_OAUTH_ACCESS_TTL", str(60 * 60)))          # 1h
REFRESH_TTL = int(os.environ.get("ATLAS_OAUTH_REFRESH_TTL", str(30 * 24 * 3600)))  # 30d
CODE_TTL = int(os.environ.get("ATLAS_OAUTH_CODE_TTL", "300"))                      # 5m
TICKET_TTL = int(os.environ.get("ATLAS_OAUTH_TICKET_TTL", "600"))                  # 10m
_LOGIN_SECRET_FILE = os.environ.get(
    "ATLAS_OAUTH_LOGIN_SECRET_FILE", str(REPO_ROOT / ".secrets" / "oauth_login_secret.txt")
)


def login_secret() -> str:
    env = os.environ.get("ATLAS_OAUTH_LOGIN_SECRET", "").strip()
    if env:
        return env
    try:
        return Path(_LOGIN_SECRET_FILE).read_text().strip()
    except OSError:
        return ""


def verify_login(secret: str) -> bool:
    expected = login_secret()
    return bool(expected) and secrets.compare_digest(secret, expected)


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class AtlasOAuthProvider(OAuthAuthorizationServerProvider):
    """SQLite-backed single-operator OAuth 2.1 authorization server."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (ticket TEXT PRIMARY KEY, client_id TEXT, params TEXT, expires_at INT);
                CREATE TABLE IF NOT EXISTS auth_codes (code TEXT PRIMARY KEY, client_id TEXT, data TEXT, expires_at INT);
                CREATE TABLE IF NOT EXISTS access_tokens (token TEXT PRIMARY KEY, client_id TEXT, scopes TEXT, expires_at INT, principal TEXT);
                CREATE TABLE IF NOT EXISTS refresh_tokens (token TEXT PRIMARY KEY, client_id TEXT, scopes TEXT, expires_at INT);
                """
            )

    # ---- clients (DCR) ----------------------------------------------------
    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        with self._conn() as c:
            row = c.execute("SELECT info FROM clients WHERE client_id=?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO clients(client_id, info) VALUES(?,?)",
                (client_info.client_id, client_info.model_dump_json()),
            )

    # ---- authorize: hand off to our single-user login --------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Persist the request under a one-time ticket and send the user to our login page."""
        ticket = secrets.token_urlsafe(32)
        payload = {
            "state": params.state,
            "scopes": list(params.scopes or []),
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        }
        with self._conn() as c:
            c.execute(
                "INSERT INTO tickets(ticket, client_id, params, expires_at) VALUES(?,?,?,?)",
                (ticket, client.client_id, json.dumps(payload), _now() + TICKET_TTL),
            )
        return f"{ISSUER_URL}{LOGIN_PATH}?ticket={urllib.parse.quote(ticket)}"

    def complete_authorization(self, ticket: str) -> Optional[str]:
        """Called by the login handler after the secret is verified. Mints an auth code
        and returns the client redirect URL (code + state). None if ticket invalid."""
        with self._conn() as c:
            row = c.execute("SELECT client_id, params, expires_at FROM tickets WHERE ticket=?", (ticket,)).fetchone()
            if not row or row["expires_at"] < _now():
                return None
            c.execute("DELETE FROM tickets WHERE ticket=?", (ticket,))
            p = json.loads(row["params"])
            code = secrets.token_urlsafe(32)
            data = {
                "scopes": p["scopes"],
                "code_challenge": p["code_challenge"],
                "redirect_uri": p["redirect_uri"],
                "redirect_uri_provided_explicitly": p["redirect_uri_provided_explicitly"],
                "resource": p["resource"],
            }
            c.execute(
                "INSERT INTO auth_codes(code, client_id, data, expires_at) VALUES(?,?,?,?)",
                (code, row["client_id"], json.dumps(data), _now() + CODE_TTL),
            )
        q = {"code": code}
        if p.get("state") is not None:
            q["state"] = p["state"]
        sep = "&" if "?" in p["redirect_uri"] else "?"
        return f"{p['redirect_uri']}{sep}{urllib.parse.urlencode(q)}"

    # ---- authorization code ----------------------------------------------
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        with self._conn() as c:
            row = c.execute(
                "SELECT data, expires_at FROM auth_codes WHERE code=? AND client_id=?",
                (authorization_code, client.client_id),
            ).fetchone()
        if not row or row["expires_at"] < _now():
            return None
        d = json.loads(row["data"])
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(d.get("scopes") or []),
            expires_at=row["expires_at"],
            client_id=client.client_id,
            code_challenge=d["code_challenge"],
            redirect_uri=d["redirect_uri"],
            redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"],
            resource=d.get("resource"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # SDK has already verified PKCE (code_verifier vs code_challenge) before this call.
        access = secrets.token_urlsafe(40)
        refresh = secrets.token_urlsafe(40)
        scopes = list(authorization_code.scopes)
        with self._conn() as c:
            c.execute("DELETE FROM auth_codes WHERE code=?", (authorization_code.code,))
            c.execute(
                "INSERT INTO access_tokens(token, client_id, scopes, expires_at, principal) VALUES(?,?,?,?,?)",
                (access, client.client_id, json.dumps(scopes), _now() + ACCESS_TTL, PRINCIPAL),
            )
            c.execute(
                "INSERT INTO refresh_tokens(token, client_id, scopes, expires_at) VALUES(?,?,?,?)",
                (refresh, client.client_id, json.dumps(scopes), _now() + REFRESH_TTL),
            )
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
            scope=" ".join(scopes) or None, refresh_token=refresh,
        )

    # ---- refresh ----------------------------------------------------------
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        with self._conn() as c:
            row = c.execute(
                "SELECT scopes, expires_at FROM refresh_tokens WHERE token=? AND client_id=?",
                (refresh_token, client.client_id),
            ).fetchone()
        if not row or row["expires_at"] < _now():
            return None
        return RefreshToken(
            token=refresh_token, client_id=client.client_id,
            scopes=json.loads(row["scopes"]), expires_at=row["expires_at"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        granted = scopes or refresh_token.scopes
        access = secrets.token_urlsafe(40)
        new_refresh = secrets.token_urlsafe(40)
        with self._conn() as c:
            c.execute("DELETE FROM refresh_tokens WHERE token=?", (refresh_token.token,))
            c.execute(
                "INSERT INTO access_tokens(token, client_id, scopes, expires_at, principal) VALUES(?,?,?,?,?)",
                (access, client.client_id, json.dumps(list(granted)), _now() + ACCESS_TTL, PRINCIPAL),
            )
            c.execute(
                "INSERT INTO refresh_tokens(token, client_id, scopes, expires_at) VALUES(?,?,?,?)",
                (new_refresh, client.client_id, json.dumps(list(granted)), _now() + REFRESH_TTL),
            )
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
            scope=" ".join(granted) or None, refresh_token=new_refresh,
        )

    # ---- access token validation (resource-server path) ------------------
    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        with self._conn() as c:
            row = c.execute(
                "SELECT client_id, scopes, expires_at FROM access_tokens WHERE token=?", (token,)
            ).fetchone()
        if not row or row["expires_at"] < _now():
            return None
        return AccessToken(
            token=token, client_id=row["client_id"],
            scopes=json.loads(row["scopes"]), expires_at=row["expires_at"], resource=RESOURCE_URL,
        )

    async def revoke_token(self, token) -> None:
        tok = getattr(token, "token", token)
        with self._conn() as c:
            c.execute("DELETE FROM access_tokens WHERE token=?", (tok,))
            c.execute("DELETE FROM refresh_tokens WHERE token=?", (tok,))

    # ---- maintenance ------------------------------------------------------
    def principal_for_token(self, token: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT principal, expires_at FROM access_tokens WHERE token=?", (token,)
            ).fetchone()
        return row["principal"] if row and row["expires_at"] >= _now() else None

    def purge_expired(self) -> None:
        now = _now()
        with self._conn() as c:
            for tbl in ("tickets", "auth_codes", "access_tokens", "refresh_tokens"):
                # never purge the long-lived local-agent token
                c.execute(
                    f"DELETE FROM {tbl} WHERE expires_at < ? AND client_id IS NOT 'local-agent'",
                    (now,),
                )

    def ensure_local_token(self) -> str:
        """Return a stable, long-lived access token for local (127.0.0.1) agents, so
        enabling OAuth doesn't break loopback callers (local automations). Minted once
        and reused; principal = the operator principal."""
        far = _now() + 10 * 365 * 24 * 3600
        with self._conn() as c:
            row = c.execute(
                "SELECT token FROM access_tokens WHERE client_id='local-agent'"
            ).fetchone()
            if row:
                return row["token"]
            tok = secrets.token_urlsafe(40)
            c.execute(
                "INSERT INTO access_tokens(token, client_id, scopes, expires_at, principal) VALUES(?,?,?,?,?)",
                (tok, "local-agent", json.dumps([]), far, PRINCIPAL),
            )
            return tok
