#!/usr/bin/env bash
# lib/70-agent-install.sh — install the coding agent this Atlas instance is
# meant to be driven by.
#
# Pluggable seam: ATLAS_AGENT selects which agent gets installed (default
# "claude"). Only "claude" is implemented today; an unknown value dies with
# a plain message rather than silently falling back.
#
# claude path: if a `claude` binary is already on PATH or at
# ~/.local/bin/claude, this is a no-op. Otherwise it runs the official
# installer AS THE INVOKING USER — never root/sudo, since the installer
# writes into that user's own account, not the system. It never attempts to
# log in: that first-login ceremony belongs to the owner, not this script.
#
# Usage: ./lib/70-agent-install.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

AGENT_KIND="${ATLAS_AGENT:-claude}"

banner "Installing coding agent (ATLAS_AGENT=${AGENT_KIND})"

claude_bin_found() {
  command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]
}

install_claude() {
  if claude_bin_found; then
    log "claude already installed, skipping"
    return 0
  fi

  if [ "$(id -u)" -eq 0 ]; then
    die "refusing to install Claude Code as root — re-run bootstrap.sh as your normal user; this phase installs into the invoking user's own account, not the system"
  fi

  log "installing Claude Code (curl -fsSL https://claude.ai/install.sh | bash)"
  if ! curl -fsSL https://claude.ai/install.sh | bash; then
    die "Claude Code install failed — run it yourself: curl -fsSL https://claude.ai/install.sh | bash (check https://docs.claude.com if that command has changed)"
  fi

  if ! claude_bin_found; then
    die "install script ran but no 'claude' binary was found (checked PATH and ~/.local/bin) — run it yourself: curl -fsSL https://claude.ai/install.sh | bash (check https://docs.claude.com if that command has changed)"
  fi

  # The installer lands in ~/.local/bin, which ~/.profile only adds to PATH
  # when the dir existed at login — a fresh box's first session misses it
  # (observed live on the 2026-08-07 tc2 round). Make the NEXT shell right.
  if ! command -v claude >/dev/null 2>&1 \
     && ! grep -q '\.local/bin' "$HOME/.bashrc" 2>/dev/null; then
    printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.bashrc"
    log "added ~/.local/bin to PATH in ~/.bashrc (new shells pick it up)"
  fi

  log "if the installer printed a note above about PATH — ignore it, that is already handled; reconnecting your SSH window picks it up"
  log "Claude Code installed"
}

case "$AGENT_KIND" in
  claude) install_claude ;;
  *) die "unknown ATLAS_AGENT '$AGENT_KIND' — only 'claude' is implemented; set ATLAS_AGENT=claude, or add support for this kind in lib/70-agent-install.sh" ;;
esac

log "agent install complete (never attempts login — that ceremony is yours)"
