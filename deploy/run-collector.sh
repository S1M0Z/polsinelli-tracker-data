#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="${POLSINELLI_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
IMAGE="${POLSINELLI_IMAGE:-polsinelli-tracker-browser:1.60.0}"
LOCK_FILE="${POLSINELLI_LOCK_FILE:-/tmp/polsinelli-collector.lock}"
RUNTIME_DIR="${POLSINELLI_RUNTIME_DIR:-/home/ubuntu/.local/share/polsinelli-collector}"
SITE_DATA_DIR="${POLSINELLI_SITE_DATA_DIR:-/var/www/polsinelli-tracker-v3/data}"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "A collection is already running; skipping."
  exit 0
fi

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

git fetch origin main
git pull --ff-only origin main

# Runtime market data lives outside Git. positions.json is refreshed from the
# publication source before every pass, while quote history and mappings persist
# locally between collections.
mkdir -p "$RUNTIME_DIR"
for file in positions.json updates.json article-state.json scan-log.json risk-policy.json; do
  cp "$file" "$RUNTIME_DIR/$file"
done
for file in quote-history.json market-data-config.json investment-view.json; do
  if [[ ! -f "$RUNTIME_DIR/$file" ]]; then
    cp "$file" "$RUNTIME_DIR/$file"
  fi
done

sudo -n docker run --rm --init --ipc=host \
  --memory=700m --memory-swap=1536m \
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
    python scripts/quote_collector_browser.py --root /runtime --session server &&
    python scripts/investment_engine.py \
      --positions /runtime/positions.json \
      --policy /runtime/risk-policy.json \
      --quotes /runtime/quote-history.json \
      --output /runtime/investment-view.json &&
    python scripts/validate_data.py --root /runtime
  '

# Publish the validated runtime snapshot directly to Nginx. GitHub is no longer
# used as a five-minute transport for quotes.
sudo -n install \
  -o ubuntu \
  -g www-data \
  -m 0644 \
  "$RUNTIME_DIR/positions.json" \
  "$SITE_DATA_DIR/positions.json"

echo "Market quotes refreshed locally; no Git commit or push performed."
