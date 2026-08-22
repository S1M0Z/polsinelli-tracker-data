#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="${POLSINELLI_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
IMAGE="polsinelli-tracker-browser:1.60.0"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this script as ubuntu; it will use passwordless sudo where needed."
  exit 1
fi

cd "$REPO_ROOT"

if [[ -z "$(sudo swapon --show --noheadings 2>/dev/null)" ]]; then
  echo "Creating a 2 GiB swap file..."
  if ! sudo fallocate -l 2G /swapfile; then
    sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=progress
  fi
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  if ! grep -qE '^/swapfile\s' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  fi
  echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-polsinelli-swap.conf >/dev/null
  sudo sysctl --system >/dev/null
else
  echo "Swap already configured; leaving it unchanged."
fi

sudo -n docker info >/dev/null
sudo -n docker build --pull \
  --file "$REPO_ROOT/Dockerfile.browser" \
  --tag "$IMAGE" \
  "$REPO_ROOT"

sudo install -m 0644 \
  "$REPO_ROOT/deploy/polsinelli-collector.service" \
  /etc/systemd/system/polsinelli-collector.service
sudo install -m 0644 \
  "$REPO_ROOT/deploy/polsinelli-collector.timer" \
  /etc/systemd/system/polsinelli-collector.timer
sudo systemctl daemon-reload

# Prepare the atomic publication layout once. Preserve an existing data
# directory as the bootstrap rollback snapshot.
SITE_DATA_DIR="${POLSINELLI_SITE_DATA_DIR:-/var/www/polsinelli-tracker-v3/data}"
PUBLIC_ROOT="$(dirname "$SITE_DATA_DIR")/.polsinelli-snapshots"
sudo mkdir -p "$PUBLIC_ROOT"
if [[ -d "$SITE_DATA_DIR" && ! -L "$SITE_DATA_DIR" ]]; then
  BOOTSTRAP="$PUBLIC_ROOT/bootstrap-$(date -u +%Y%m%dT%H%M%SZ)"
  sudo mv "$SITE_DATA_DIR" "$BOOTSTRAP"
  sudo ln -s "$BOOTSTRAP" "$SITE_DATA_DIR"
fi

printf '\nInstallation prepared. The timer is intentionally still disabled.\n'
printf 'Manual test: FORCE_RUN=1 %q/deploy/run-collector.sh\n' "$REPO_ROOT"
printf 'Enable after the Git remote can push: sudo systemctl enable --now polsinelli-collector.timer\n'
