#!/usr/bin/env bash
# Phase 3 on the edge VM: install Anubis (proof-of-work gate) and Vector (log
# shipper), then switch Caddy to send pages through Anubis.
#
#   infra/azure/edge/phase3.sh            (from the Codespace; safe to re-run)
#
# Needs, in this order:
#   1. infra/gcp: `terraform apply` done (bucket + Vector's service account)
#   2. infra/azure: `terraform apply` done (renders the new Caddyfile output)
#   3. gcloud logged in, ssh to antiseo-edge working (api/README.md step 1)
#
# Packages are pinned by version AND sha256, so a tampered download is refused.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EDGE="${EDGE:-azureuser@antiseo-edge}"
SITE_HOST="${SITE_HOST:-search.theincorrect.one}"
APP_TARGET="${APP_TARGET:-http://10.20.2.4:8080}"

ANUBIS_VER=1.28.2
ANUBIS_SHA=fbcd0f3ec8cad426eacc1873b0073285d824dafb4163fcfe3e1261f7d8190701
VECTOR_VER=0.61.0
VECTOR_SHA=1cf2bc77f865e05e539c177feb0eb5feee20ee2762ea379f101610c9625fc4b8

BUCKET="$(cd "$HERE/../../gcp" && terraform output -raw edge_logs_bucket)"
VECTOR_SA="$(cd "$HERE/../../gcp" && terraform output -raw vector_service_account)"
CADDYFILE="$(cd "$HERE/.." && terraform output -raw edge_caddyfile)"
grep -q "127.0.0.1:8923" <<<"$CADDYFILE" || {
  echo "The Caddyfile output doesn't route to Anubis yet: run 'terraform apply' in infra/azure first."; exit 1; }

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
chmod 700 "$STAGE"

echo "== render configs"
# The probe regex lives in one file; Anubis denies it and the SIEM counts it.
python3 - "$HERE/phase3" "$STAGE" <<'PY'
import sys, pathlib
src, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
rx = (src / "probe-regex.txt").read_text().strip()
assert "'" not in rx, "probe regex must not contain a single quote (YAML quoting)"
policy = (src / "anubis-policy.yaml").read_text().replace("__PROBE_REGEX__", rx)
assert "__PROBE_REGEX__" not in policy
(out / "anubis-policy.yaml").write_text(policy)
(out / "vector.yaml").write_text((src / "vector.yaml").read_text())
PY

echo "== Vector's GCS key"
if ssh "$EDGE" 'sudo test -s /etc/vector/gcs-key.json'; then
  echo "edge already has a key; keeping it"
else
  # A key for a service account that can only CREATE objects in one bucket.
  # It goes straight to the edge and is deleted here when this script exits.
  gcloud iam service-accounts keys create "$STAGE/gcs-key.json" --iam-account "$VECTOR_SA" --quiet
fi

echo "== upload"
tar -C "$STAGE" -czf - . | ssh "$EDGE" 'rm -rf /tmp/p3 && mkdir -m 700 /tmp/p3 && tar -C /tmp/p3 -xzf -'

echo "== install on the edge"
ssh "$EDGE" "sudo ANUBIS_VER=$ANUBIS_VER ANUBIS_SHA=$ANUBIS_SHA VECTOR_VER=$VECTOR_VER VECTOR_SHA=$VECTOR_SHA \
  SITE_HOST=$SITE_HOST APP_TARGET=$APP_TARGET BUCKET=$BUCKET bash -s" <<'REMOTE'
set -euo pipefail
cd /tmp/p3

fetch() {  # fetch <url> <file> <sha256>
  curl -fsSL -o "$2" "$1"
  echo "$3  $2" | sha256sum -c --quiet - || { echo "!! checksum mismatch for $2"; exit 1; }
}

# ---- Anubis ----
if [ "$(dpkg-query -W -f='${Version}' anubis 2>/dev/null || true)" != "$ANUBIS_VER" ]; then
  fetch "https://github.com/TecharoHQ/anubis/releases/download/v$ANUBIS_VER/anubis_${ANUBIS_VER}_amd64.deb" anubis.deb "$ANUBIS_SHA"
  apt-get install -y -q ./anubis.deb
fi
install -m 0644 anubis-policy.yaml /etc/anubis/searchbox.botPolicies.yaml
# Keep the signing key across re-runs, so visitors' pass cookies stay valid.
KEYHEX="$(sed -n 's/^ED25519_PRIVATE_KEY_HEX=//p' /etc/anubis/searchbox.env 2>/dev/null || true)"
[ -n "$KEYHEX" ] || KEYHEX="$(openssl rand -hex 32)"
umask 077
cat > /etc/anubis/searchbox.env <<EOF
BIND=127.0.0.1:8923
BIND_NETWORK=tcp
TARGET=$APP_TARGET
POLICY_FNAME=/etc/anubis/searchbox.botPolicies.yaml
METRICS_BIND=127.0.0.1:9090
METRICS_BIND_NETWORK=tcp
REDIRECT_DOMAINS=$SITE_HOST
SERVE_ROBOTS_TXT=true
ED25519_PRIVATE_KEY_HEX=$KEYHEX
EOF
umask 022
systemctl enable anubis@searchbox.service >/dev/null 2>&1
systemctl restart anubis@searchbox.service
for i in $(seq 1 20); do
  if curl -fsS -H 'X-Real-Ip: 127.0.0.1' http://127.0.0.1:8923/ 2>/dev/null | grep -q "not a bot"; then break; fi
  [ "$i" = 20 ] && { echo "!! Anubis didn't come up:"; journalctl -u anubis@searchbox -n 20 --no-pager; exit 1; }
  sleep 1
done
echo "anubis: serving challenges on 127.0.0.1:8923"

# ---- Vector ----
if [ "$(dpkg-query -W -f='${Version}' vector 2>/dev/null || true)" != "$VECTOR_VER-1" ]; then
  fetch "https://github.com/vectordotdev/vector/releases/download/v$VECTOR_VER/vector_${VECTOR_VER}-1_amd64.deb" vector.deb "$VECTOR_SHA"
  apt-get install -y -q ./vector.deb
fi
install -m 0644 vector.yaml /etc/vector/vector.yaml
if [ -f gcs-key.json ]; then
  install -o root -g vector -m 0640 gcs-key.json /etc/vector/gcs-key.json
fi
printf 'GCS_BUCKET=%s\n' "$BUCKET" > /etc/default/vector
chmod 0644 /etc/default/vector
install -d -o vector -g vector -m 0750 /var/lib/vector
# Vector reads Caddy's log (group caddy) and journald (the package adds systemd-journal).
usermod -aG caddy vector
chgrp caddy /var/log/caddy/access*.json 2>/dev/null || true
chmod 0640 /var/log/caddy/access*.json 2>/dev/null || true
sudo -u vector env GCS_BUCKET="$BUCKET" vector validate --no-environment /etc/vector/vector.yaml
systemctl enable vector >/dev/null 2>&1
systemctl restart vector
sleep 3
systemctl is-active --quiet vector || { echo "!! Vector didn't start:"; journalctl -u vector -n 30 --no-pager; exit 1; }
echo "vector: shipping to gs://$BUCKET/edge/ every 15 min"

rm -rf /tmp/p3
REMOTE

echo "== Caddy: pages now go through Anubis"
"$HERE/deploy.sh"

echo
echo "Done. Check:"
echo "  curl -s https://$SITE_HOST/ | grep -o '<title>[^<]*'      # the Anubis challenge page"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' https://$SITE_HOST/api/v1/health   # 200, no challenge"
echo "  gcloud storage ls gs://$BUCKET/edge/                      # first file within ~15 min"
