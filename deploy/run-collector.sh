#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="${POLSINELLI_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
IMAGE="${POLSINELLI_IMAGE:-polsinelli-tracker-browser:1.60.0}"
LOCK_FILE="${POLSINELLI_LOCK_FILE:-/tmp/polsinelli-collector.lock}"
CONTAINER_NAME="${POLSINELLI_CONTAINER_NAME:-polsinelli-quote-collector}"
RUNTIME_DIR="${POLSINELLI_RUNTIME_DIR:-/home/ubuntu/.local/share/polsinelli-collector}"
SITE_DATA_DIR="${POLSINELLI_SITE_DATA_DIR:-/var/www/polsinelli-tracker-v3/data}"
COLLECTOR_TIMEOUT_SECONDS="${POLSINELLI_COLLECTOR_TIMEOUT_SECONDS:-1500}"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "A collection is already running; skipping."
  exit 0
fi

# A systemd timeout kills the Docker client, not necessarily the container it
# started. Always remove a stale instance before a pass and clean up on exit.
cleanup_container() {
  sudo -n docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup_container EXIT INT TERM
cleanup_container

cd "$REPO_ROOT"

# The timer itself runs continuously; avoid starting Chromium outside the broad
# European trading window unless FORCE_RUN=1 is supplied for a manual test.
if [[ "${FORCE_RUN:-0}" != "1" ]]; then
  weekday="$(date -u +%u)"
  hour="$(date -u +%H)"
  if (( 10#$weekday > 5 || 10#$hour < 7 || 10#$hour > 18 )); then
    echo "Outside collection window; skipping."
    exit 0
  fi
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Repository has uncommitted changes; refusing to overwrite them."
  git status --short
  exit 1
fi

git pull --ff-only origin main

# Runtime market data lives outside Git. positions.json is refreshed from the
# publication source before every pass, while quote history and mappings persist
# locally between collections.
mkdir -p "$RUNTIME_DIR"
# Configuration is versioned; all mutable operational state persists in runtime.
for file in risk-policy.json public-manifest.json; do
  cp "$file" "$RUNTIME_DIR/$file"
done
for file in positions.json updates.json article-state.json scan-log.json quote-history.json market-data-config.json investment-view.json; do
  if [[ ! -f "$RUNTIME_DIR/$file" ]]; then
    cp "$file" "$RUNTIME_DIR/$file"
  fi
done
python3 scripts/merge_market_config.py \
  --baseline "$REPO_ROOT/market-data-config.json" \
  --runtime "$RUNTIME_DIR/market-data-config.json"
python3 scripts/migrate_schema.py --root "$RUNTIME_DIR"

timeout --signal=TERM --kill-after=30s "${COLLECTOR_TIMEOUT_SECONDS}s" \
sudo -n docker run --rm --init --ipc=host \
  --name "$CONTAINER_NAME" \
  --cpus=1.0 --pids-limit=128 \
  --memory=512m --memory-swap=768m \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --env MARKET_DATA_PROVIDER=euronext \
  --env EURONEXT_DEFAULT_MIC=XMLI \
  --env EURONEXT_LOCALE=en \
  --volume "$REPO_ROOT:/app" \
  --volume "$RUNTIME_DIR:/runtime" \
  --workdir /app \
  "$IMAGE" \
  bash -lc '
    zone_status=0
    python scripts/sync_zonebourse_browser.py --root /runtime --max-article-fetches 12 || zone_status=$?
    if (( zone_status != 0 && zone_status != 2 )); then
      exit "$zone_status"
    fi
    python scripts/quote_collector_browser.py --root /runtime --session server &&
    python scripts/investment_engine.py \
      --positions /runtime/positions.json \
      --policy /runtime/risk-policy.json \
      --quotes /runtime/quote-history.json \
      --output /runtime/investment-view.json &&
    python scripts/validate_data.py --root /runtime
  '

# Build and validate one immutable snapshot before publishing any file.
SNAPSHOT_ROOT="${POLSINELLI_SNAPSHOT_ROOT:-$RUNTIME_DIR/snapshots}"
mkdir -p "$SNAPSHOT_ROOT"
SNAPSHOT_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
SNAPSHOT_DIR="$SNAPSHOT_ROOT/$SNAPSHOT_ID"
python3 scripts/build_snapshot.py --source "$RUNTIME_DIR" --output "$SNAPSHOT_DIR"
python3 scripts/validate_data.py --root "$SNAPSHOT_DIR"

# SITE_DATA_DIR is a symlink switched atomically; old snapshots enable rollback.
PUBLIC_ROOT="$(dirname "$SITE_DATA_DIR")/.polsinelli-snapshots"
sudo -n mkdir -p "$PUBLIC_ROOT"
PUBLIC_SNAPSHOT="$PUBLIC_ROOT/$SNAPSHOT_ID"
sudo -n mkdir "$PUBLIC_SNAPSHOT"
while IFS= read -r file; do
  sudo -n install -o ubuntu -g www-data -m 0644 "$SNAPSHOT_DIR/$file" "$PUBLIC_SNAPSHOT/$file"
done < <(python3 -c 'import json; print("\n".join(json.load(open("public-manifest.json"))["files"]))')
sudo -n ln -sfn "$PUBLIC_SNAPSHOT" "$SITE_DATA_DIR.new"
sudo -n mv -Tf "$SITE_DATA_DIR.new" "$SITE_DATA_DIR"

echo "Market quotes refreshed locally; no Git commit or push performed."
