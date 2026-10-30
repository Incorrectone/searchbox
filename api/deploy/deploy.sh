#!/usr/bin/env bash
# Build the API and deploy it to the app VM over Tailscale SSH.
#
#   api/deploy/deploy.sh [path/to/models/minilm]
#
# Needs: the .NET 10 SDK and a Codespace logged in to Tailscale (see api/README.md).
# Each deploy goes into its own folder under /opt/searchbox/releases/; the
# "current" symlink is switched only after the new build answers /api/v1/health,
# otherwise the previous release is put back.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${HOST:-azureuser@antiseo-app}"
MODELS="${1:-$HERE/../../models/minilm}"
REL="$(date +%Y%m%d-%H%M%S)"
OUT="$(mktemp -d)"

for f in model.onnx vocab.txt; do
  [ -f "$MODELS/$f" ] || { echo "missing $MODELS/$f (see api/README.md, 'Model files')"; exit 1; }
done

echo "== build (linux-arm64, self-contained: the VM needs no .NET install)"
dotnet publish "$HERE/../SearchBox.Api/SearchBox.Api.csproj" -c Release -r linux-arm64 \
  --self-contained true -p:PublishSingleFile=false -o "$OUT/app" --nologo -v quiet
mkdir -p "$OUT/models" && cp "$MODELS/model.onnx" "$MODELS/vocab.txt" "$OUT/models/"
cp "$HERE/searchbox-api.service" "$OUT/"

echo "== upload"
tar -C "$OUT" -czf - . | ssh "$HOST" "rm -rf /tmp/sb-$REL && mkdir -p /tmp/sb-$REL && tar -C /tmp/sb-$REL -xzf -"

echo "== install release $REL"
ssh "$HOST" "sudo bash -s" <<REMOTE
set -euo pipefail
id searchbox >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin searchbox
mkdir -p /opt/searchbox/releases /opt/searchbox/models/minilm /etc/searchbox
if [ ! -f /etc/searchbox/api.env ]; then
  echo "!! /etc/searchbox/api.env is missing: create it from api/deploy/api.env.example first"; exit 1
fi
chown root:searchbox /etc/searchbox/api.env && chmod 0640 /etc/searchbox/api.env
mv /tmp/sb-$REL/app /opt/searchbox/releases/$REL
# root owns the code; everyone may read it and enter its folders (umask- and tar-proof)
chown -R root:root /opt/searchbox/releases/$REL
chmod -R u=rwX,go=rX /opt/searchbox/releases/$REL
chmod 0755 /opt/searchbox /opt/searchbox/releases /opt/searchbox/models /opt/searchbox/models/minilm
install -m 0644 /tmp/sb-$REL/models/* /opt/searchbox/models/minilm/
install -m 0644 /tmp/sb-$REL/searchbox-api.service /etc/systemd/system/searchbox-api.service
rm -rf /tmp/sb-$REL
PREV="\$(readlink -e /opt/searchbox/current 2>/dev/null || true)"   # -e: empty if there is no previous release
ln -sfn /opt/searchbox/releases/$REL /opt/searchbox/current
systemctl daemon-reload
systemctl enable searchbox-api >/dev/null 2>&1
systemctl restart searchbox-api
for i in \$(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/api/v1/health >/dev/null 2>&1; then
    echo "healthy: \$(curl -s http://127.0.0.1:8080/api/v1/health)"
    ls -1dt /opt/searchbox/releases/* | tail -n +4 | xargs -r rm -rf   # keep the last 3 releases
    exit 0
  fi
  sleep 1
done
echo "!! new release did not become healthy; recent logs:"
journalctl -u searchbox-api -n 30 --no-pager || true
if [ -n "\$PREV" ]; then
  ln -sfn "\$PREV" /opt/searchbox/current && systemctl restart searchbox-api && echo "rolled back to \$PREV"
fi
exit 1
REMOTE
rm -rf "$OUT"
echo "== done: release $REL"
