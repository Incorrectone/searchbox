#!/usr/bin/env bash
# Push a changed Caddyfile to the edge VM without rebuilding it.
# Run from infra/azure in a Codespace that is logged in to Tailscale
# (see api/README.md, "Deploying"). Caddy validates before reloading, so a
# broken config never replaces a working one.
set -euo pipefail
cd "$(dirname "$0")/.."
terraform output -raw edge_caddyfile | ssh azureuser@antiseo-edge '
  sudo install -o root -g caddy -m 0640 /dev/stdin /etc/caddy/Caddyfile.new &&
  sudo -u caddy -H caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new &&
  sudo mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile &&
  sudo systemctl reload caddy &&
  echo "Caddy reloaded."'
