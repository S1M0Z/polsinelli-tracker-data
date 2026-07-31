#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="${POLSINELLI_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
IMAGE="${POLSINELLI_IMAGE:-polsinelli-tracker-browser:1.60.0}"
LOCK_FILE="${POLSINELLI_LOCK_FILE:-/tmp/polsinelli-collector.lock}"

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

sudo -n docker run --rm --init --ipc=host \
  --memory=700m --memory-swap=1536m \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --env MARKET_DATA_PROVIDER=euronext \
  --env EURONEXT_DEFAULT_MIC=XMLI \
  --env EURONEXT_LOCALE=en \
  --volume "$REPO_ROOT:/app" \
  --workdir /app \
  "$IMAGE" \
  bash -lc '
    python scripts/quote_collector_browser.py --session server &&
    python scripts/investment_engine.py &&
    python scripts/validate_data.py
  '

git add positions.json quote-history.json market-data-config.json investment-view.json
if git diff --cached --quiet; then
  echo "No market-data changes to publish."
  exit 0
fi

git config user.name "polsinelli-server-bot"
git config user.email "server-bot@users.noreply.github.com"
git commit -m "Actualise les cotations Euronext depuis le serveur"

# Incorporate any publication scan committed while Chromium was running.
git pull --rebase origin main
git push origin main
