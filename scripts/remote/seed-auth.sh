#!/usr/bin/env bash
# scripts/remote/seed-auth.sh — seed worker auth + Claude/git config into
# a provisioned Fly.io Machine.
#
# This is the remote equivalent of the `AUTH_MOUNTS` bind-mount block in the
# local `nerdctl run` path (pila launcher lines 542–726). Instead of mounting
# a $STAGE scratch dir, the same content is delivered over SSH via flyctl.
#
# Usage (invoked from the pila launcher's RUNTIME=fly branch after
# provision_machine() returns successfully):
#
#   source scripts/remote/seed-auth.sh
#   seed_auth              # blocks until seeding is complete
#
# Environment variables (must be set by the launcher before sourcing):
#
#   PILA_MACHINE_ID  — ID of the provisioned Fly Machine (set by provision.sh)
#   PILA_FLY_APP     — Fly.io app name (default: "pila"; same as provision.sh)
#   STAGE            — host-side scratch dir already assembled by the launcher
#                      containing .claude/, .claude.json, and optional .gitconfig
#   HOME             — standard; used to read host git identity
#
# What is seeded:
#   1. ~/.claude.json (projects-stripped copy from $STAGE/.claude.json)
#   2. ~/.claude/ capability dirs (from $STAGE/.claude/, excluding
#      session/history/bulk dirs already filtered during $STAGE assembly)
#   3. ~/.claude/.credentials.json — if present in $STAGE (Keychain-extracted
#      on macOS), or constructed from $CLAUDE_CODE_OAUTH_TOKEN (Linux / fallback)
#   4. git identity: user.name and user.email from the host's git config,
#      set globally on the remote machine so worker commits have a valid author.
#
# Auth credential notes:
#   On macOS the launcher extracts the OAuth token from Keychain and writes it
#   to $STAGE/.claude/.credentials.json before this script runs; the tar pipe
#   in step 2 delivers that file along with the rest of ~/.claude/.
#   On Linux (or when Keychain extraction fails), the token lives in
#   $CLAUDE_CODE_OAUTH_TOKEN. In that case seed_auth() writes a minimal
#   credentials JSON to the machine directly — the same single-token JSON the
#   Claude Code CLI reads from ~/.claude/.credentials.json on Linux.
#
# Seeding mechanism:
#   Files are delivered using `flyctl machine exec` with a tar pipe:
#       tar -cC "$STAGE" . | flyctl machine exec --stdin ...  tar -xC /home/pila
#   This is the only approach that (a) doesn't require an open SSH port,
#   (b) works without a running sshd inside the machine, and (c) preserves
#   file permissions (mode 0600 on credentials, 0700 on ~/.ssh / ~/.gnupg).

set -euo pipefail

FLY_APP="${PILA_FLY_APP:-pila}"

# --- seed_auth -----------------------------------------------------------
# Seeds Claude config + git identity into the provisioned Fly Machine.
# Requires: $PILA_MACHINE_ID (from provision.sh), $STAGE (from launcher),
#           $FLY_APP, $HOME, and either $STAGE/.claude/.credentials.json or
#           $CLAUDE_CODE_OAUTH_TOKEN.
# Returns: 0 on success; 1 on failure (caller should abort the run).
seed_auth() {
  local machine_id="${PILA_MACHINE_ID:-}"
  if [ -z "$machine_id" ]; then
    echo "pila: seed_auth: PILA_MACHINE_ID is not set — cannot seed" >&2
    return 1
  fi
  if [ -z "${STAGE:-}" ]; then
    echo "pila: seed_auth: STAGE is not set — launcher must assemble the scratch dir first" >&2
    return 1
  fi

  echo "[pila] remote: seeding Claude config + git identity into machine $machine_id ..." >&2

  # --- 1. Seed ~/.claude.json + ~/.claude/ via a single tar pipe ----------
  # The $STAGE dir already has:
  #   .claude.json           (projects-stripped)
  #   .claude/               (bulk/history dirs excluded; settings.json.* stripped)
  #   .claude/.credentials.json  (if Keychain-extracted on macOS)
  # We pipe the whole $STAGE tree (limited to the claude files) as a tar
  # stream into `tar -xC /home/pila` on the remote, preserving permissions.
  #
  # We explicitly exclude git/ssh/gnupg material — those are git-push auth
  # which lives on the host per DESIGN §6 *Finalization*. Workers only need
  # Claude auth + git identity; SSH keys for pushing are the host's concern.
  if ! tar -cC "$STAGE" \
       --exclude='.gitconfig' \
       --exclude='.gitconfig.local' \
       --exclude='.gitignore' \
       --exclude='.gitignore_global' \
       --exclude='.git-credentials' \
       --exclude='.netrc' \
       --exclude='.ssh' \
       --exclude='.gnupg' \
       --exclude='.config' \
       . \
       | flyctl machine exec "$machine_id" \
           --app "$FLY_APP" \
           --stdin \
           -- tar -xC /home/pila 2>&1; then
    echo "pila: seed_auth: failed to seed Claude config files into machine $machine_id" >&2
    return 1
  fi
  echo "[pila] remote: Claude config seeded" >&2

  # --- 2. Token fallback: if no credentials file was in $STAGE, write one --
  # On Linux (and on macOS when Keychain extraction fails) the launcher does
  # not write $STAGE/.claude/.credentials.json, but it may set
  # $CLAUDE_CODE_OAUTH_TOKEN. In that case, write a minimal credentials JSON
  # directly to the machine so `claude -p` can authenticate.
  #
  # The file format mirrors what the macOS Keychain stores and what the Linux
  # CLI reads: {"claudeAiOauth":{"accessToken":"..."}}.
  if [ ! -s "$STAGE/.claude/.credentials.json" ] && \
     [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    local creds_json
    creds_json="$(printf '{"claudeAiOauth":{"accessToken":"%s"}}' \
                         "$CLAUDE_CODE_OAUTH_TOKEN")"
    if ! printf '%s' "$creds_json" \
         | flyctl machine exec "$machine_id" \
             --app "$FLY_APP" \
             --stdin \
             -- sh -c 'cat > /home/pila/.claude/.credentials.json && chmod 600 /home/pila/.claude/.credentials.json' 2>&1; then
      echo "pila: seed_auth: failed to write credentials JSON from CLAUDE_CODE_OAUTH_TOKEN" >&2
      return 1
    fi
    echo "[pila] remote: Claude credentials written from CLAUDE_CODE_OAUTH_TOKEN" >&2
  elif [ ! -s "$STAGE/.claude/.credentials.json" ] && \
       [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "pila: seed_auth: no credentials available — neither \$STAGE/.claude/.credentials.json" >&2
    echo "  nor \$CLAUDE_CODE_OAUTH_TOKEN is set. Workers will not be able to authenticate." >&2
    echo "  On macOS: grant the launcher Keychain access (the prompt that appears on first run)." >&2
    echo "  On Linux: export CLAUDE_CODE_OAUTH_TOKEN in your shell before running pila." >&2
    return 1
  fi

  # --- 3. Set git identity on the remote machine -------------------------
  # Read user.name and user.email from the host git config and set them
  # globally on the machine. Workers commit as the host user.
  local git_name git_email
  git_name="$(git config user.name 2>/dev/null || true)"
  git_email="$(git config user.email 2>/dev/null || true)"

  if [ -z "$git_name" ] || [ -z "$git_email" ]; then
    echo "pila: seed_auth: git user.name or user.email is not configured on the host." >&2
    echo "  Run: git config --global user.name \"Your Name\"" >&2
    echo "       git config --global user.email \"you@example.com\"" >&2
    return 1
  fi

  if ! flyctl machine exec "$machine_id" \
         --app "$FLY_APP" \
         -- git config --global user.name "$git_name" 2>&1; then
    echo "pila: seed_auth: failed to set git user.name on machine $machine_id" >&2
    return 1
  fi
  if ! flyctl machine exec "$machine_id" \
         --app "$FLY_APP" \
         -- git config --global user.email "$git_email" 2>&1; then
    echo "pila: seed_auth: failed to set git user.email on machine $machine_id" >&2
    return 1
  fi

  echo "[pila] remote: git identity set (${git_name} <${git_email}>)" >&2
  echo "[pila] remote: seed_auth complete" >&2
  return 0
}
