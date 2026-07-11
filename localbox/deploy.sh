#!/usr/bin/env bash
#
# deploy.sh — build & deploy localbox to an Unraid box over SSH, then verify.
#
# What it does:
#   1. Preflight: SSH reachable, docker present, music share exists.
#   2. Ship this folder's source to the box (tar over SSH — no rsync needed).
#   3. Build the image on the box.
#   4. Recreate the container with the correct mounts (music read-only).
#   5. Health-check: container running + /healthy responding + library readable.
#
# Usage (note: in zsh, don't paste a trailing "# comment" — zsh passes it as an
# argument. The script now ignores a first arg starting with '#' just in case.)
#   ./deploy.sh
#   SSH_TARGET=root@10.0.23.105 PORT=8080 ./deploy.sh
#   ./deploy.sh root@10.0.23.105
#
set -euo pipefail

# ----------------------------------------------------------------------------
# Config (override via env or first arg)
# ----------------------------------------------------------------------------
# Ignore a first arg that is empty or a pasted shell comment (zsh quirk).
ARG="${1:-}"
case "$ARG" in ''|'#'*) ARG="" ;; esac
SSH_TARGET="${ARG:-${SSH_TARGET:-root@10.0.23.105}}"
HOST="${SSH_TARGET#*@}"

IMAGE="${IMAGE:-localbox:latest}"
CONTAINER="${CONTAINER:-localbox}"
PORT="${PORT:-8085}"

MUSIC_DIR="${MUSIC_DIR:-/mnt/user/music}"                    # on the Unraid box, read-only
DATA_DIR="${DATA_DIR:-/mnt/user/appdata/localbox}"           # analysis + transcode cache
SRC_DIR="${SRC_DIR:-~/source/dale/Infinite/localbox}"        # where source is shipped & built

# The path may use ~ ; expand it on the *remote* side (its $HOME), not locally.
case "$SRC_DIR" in
  "~/"*) REMOTE_SRC="\$HOME/${SRC_DIR#\~/}" ;;
  "~")   REMOTE_SRC="\$HOME" ;;
  *)     REMOTE_SRC="$SRC_DIR" ;;
esac

ACOUSTID_KEY="${ACOUSTID_KEY:-}"
FORCE_TRANSCODE="${FORCE_TRANSCODE:-}"

# Connection multiplexing: authenticate ONCE (even with a passphrase-protected
# key or password auth), then reuse the same connection for every remote call.
# Note: no BatchMode — we *want* to allow a single interactive passphrase prompt.
SSH_CTRL="/tmp/localbox-cm-%r-%h-%p"
SSH_OPTS="${SSH_OPTS:--o ConnectTimeout=10 -o ControlMaster=auto -o ControlPath=$SSH_CTRL -o ControlPersist=300}"

# Where this script (and the source it deploys) lives.
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------------------------------------------------------
# Pretty output
# ----------------------------------------------------------------------------
BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; NC=$'\033[0m'
step() { printf '\n%s==>%s %s%s\n' "$BOLD" "$NC" "$1" "$NC"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$NC" "$1"; }
die()  { printf '  %s✗ %s%s\n' "$RED" "$1" "$NC" >&2; exit 1; }

# Shorthand: run a command on the Unraid box.
remote() { ssh $SSH_OPTS "$SSH_TARGET" "$@"; }

# Close the shared master connection when the script exits.
cleanup() { ssh $SSH_OPTS -O exit "$SSH_TARGET" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# ----------------------------------------------------------------------------
step "Preflight — $SSH_TARGET"
# ----------------------------------------------------------------------------
[ -f "$LOCAL_DIR/Dockerfile" ] || die "Dockerfile not found in $LOCAL_DIR"

printf '  %sconnecting — you may be prompted for your SSH key passphrase once%s\n' "$DIM" "$NC"
remote true || die "cannot SSH to $SSH_TARGET (install your key with: ssh-copy-id $SSH_TARGET , or load it with: ssh-add)"
ok "SSH connection works (multiplexed)"

remote 'command -v docker >/dev/null' || die "docker not found on the remote host"
ok "docker present: $(remote 'docker --version')"

if remote "[ -d '$MUSIC_DIR' ]"; then
  ok "music library found at $MUSIC_DIR"
else
  die "music library $MUSIC_DIR does not exist on the box (set MUSIC_DIR=...)"
fi

remote "mkdir -p '$DATA_DIR' \"$REMOTE_SRC\""
ok "cache dir $DATA_DIR and source dir $SRC_DIR ready"

# ----------------------------------------------------------------------------
step "Shipping source to $SRC_DIR"
# ----------------------------------------------------------------------------
# tar-pipe: bundle the source locally (excluding junk) and unpack on the box.
tar -C "$LOCAL_DIR" \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
    -czf - . | remote "tar -xzf - -C \"$REMOTE_SRC\""
ok "source copied"

# ----------------------------------------------------------------------------
step "Building image $IMAGE on the box (first build pulls Python/librosa — may take a few minutes)"
# ----------------------------------------------------------------------------
remote "cd \"$REMOTE_SRC\" && docker build -t '$IMAGE' ." || die "docker build failed"
ok "image built"

# ----------------------------------------------------------------------------
step "Recreating container $CONTAINER"
# ----------------------------------------------------------------------------
remote "docker rm -f '$CONTAINER' >/dev/null 2>&1 || true"
remote "docker run -d \
  --name '$CONTAINER' \
  --restart unless-stopped \
  -p '${PORT}:8080' \
  -e ACOUSTID_KEY='$ACOUSTID_KEY' \
  -e FORCE_TRANSCODE='$FORCE_TRANSCODE' \
  -v '$MUSIC_DIR:/music:ro' \
  -v '$DATA_DIR:/data' \
  '$IMAGE'" >/dev/null || die "docker run failed"
ok "container started"

# ----------------------------------------------------------------------------
step "Verifying deployment"
# ----------------------------------------------------------------------------
# 1) container state
sleep 2
STATE="$(remote "docker inspect -f '{{.State.Status}}' '$CONTAINER'" 2>/dev/null || echo unknown)"
if [ "$STATE" = "running" ]; then
  ok "container state: running"
else
  warn "container state: $STATE — recent logs:"
  remote "docker logs --tail 40 '$CONTAINER'" || true
  die "container is not running"
fi

# 2) HTTP health — poll /healthy for up to ~60s (uvicorn + first import can be slow)
HEALTH_URL="http://${HOST}:${PORT}/healthy"
printf '  %s…%s waiting for %s\n' "$DIM" "$NC" "$HEALTH_URL"
HEALTHY=0
for i in $(seq 1 30); do
  # Prefer checking from this machine; fall back to curl inside the box.
  if command -v curl >/dev/null && curl -fsS --max-time 3 "$HEALTH_URL" >/tmp/localbox_health 2>/dev/null; then
    HEALTHY=1; break
  fi
  if remote "curl -fsS --max-time 3 http://localhost:${PORT}/healthy" >/tmp/localbox_health 2>/dev/null; then
    HEALTHY=1; break
  fi
  sleep 2
done

if [ "$HEALTHY" = "1" ]; then
  ok "health endpoint OK: $(cat /tmp/localbox_health)"
else
  warn "health endpoint did not respond in time — recent logs:"
  remote "docker logs --tail 40 '$CONTAINER'" || true
  die "deployment unhealthy"
fi

# 3) library is actually readable through the app
if remote "curl -fsS --max-time 5 'http://localhost:${PORT}/api/library'" >/tmp/localbox_lib 2>/dev/null; then
  FOLDERS=$(remote "curl -fsS 'http://localhost:${PORT}/api/library'" | tr ',' '\n' | grep -c '"name"' || true)
  ok "library API OK (top level lists ~${FOLDERS} entries)"
else
  warn "library API /api/library did not respond cleanly (check the $MUSIC_DIR mount)"
fi

# 4) docker's own healthcheck (may still be 'starting' briefly)
HC="$(remote "docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' '$CONTAINER'" 2>/dev/null || echo none)"
[ "$HC" != "none" ] && ok "docker healthcheck: $HC"

printf '\n%s✓ localbox deployed and healthy%s\n' "$GREEN$BOLD" "$NC"
printf '  Web UI:  %shttp://%s:%s/%s\n' "$BOLD" "$HOST" "$PORT" "$NC"
printf '  Logs:    ssh %s docker logs -f %s\n' "$SSH_TARGET" "$CONTAINER"
printf '  Restart: ssh %s docker restart %s\n\n' "$SSH_TARGET" "$CONTAINER"
