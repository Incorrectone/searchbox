# SearchBox API (Phase 2)

ASP.NET Core 10 API + retro web UI, running on the app VM (`10.20.2.4:8080`), behind Caddy on the edge VM.

- **How the code works (C#/.NET guide):** [`HOW-IT-WORKS.md`](HOW-IT-WORKS.md)
- **Endpoints:** `GET /api/v1/search`, `/images`, `/summary`, `/stats`, `/health`, and `/openapi/v1.json`

```
api/
  SearchBox.Api/            the app (Program.cs, Endpoints.cs, Data/, Embedding/, Limits/, Caching/, Summary/, wwwroot/)
  SearchBox.Api.Tests/      xUnit tests  (dotnet test)
  deploy/                   systemd unit, api.env template, deploy.sh
infra/azure/
  storage.tf                thumbnail storage + SAS tokens
  edge.tf, edge/, cloud-init/edge.yaml   edge VM, Caddyfile template, fallback page
db/002_images.sql           images table
```

---

## One-time setup (in this order)

### 1. Codespace tools
Replace `.devcontainer/devcontainer.json` with:
```json
{
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu",
  "features": {
    "ghcr.io/devcontainers/features/terraform:1": {},
    "ghcr.io/devcontainers/features/azure-cli:1": {},
    "ghcr.io/devcontainers/features/dotnet:2": { "version": "10.0" },
    "ghcr.io/devcontainers/features/python:1": { "version": "3.13" },
    "ghcr.io/tailscale/codespace/tailscale": {}
  },
  "customizations": { "vscode": { "extensions": ["hashicorp.terraform", "ms-dotnettools.csdevkit"] } }
}
```
Rebuild the Codespace (Command Palette → *Codespaces: Rebuild Container*). Then join your tailnet **as yourself**, which makes the Codespace an admin device that the ACL lets SSH into the VMs:
```bash
sudo tailscale up --accept-routes      # opens a login link
ssh azureuser@antiseo-app 'hostname'   # should print the VM name
```

### 2. Model files: make once, share everywhere
The crawler and the API must use the **identical** `model.onnx`. Make it once in the Codespace and publish it as a GitHub Release asset:
```bash
cd /workspaces/searchbox
echo "models/" >> .gitignore
pip install -r crawler/requirements.txt
cd crawler && MODEL_DIR=../models/minilm python -m antiseo_crawler prepare-model && cd ..
gh release create model-v1 models/minilm/model.onnx models/minilm/tokenizer.json models/minilm/vocab.txt \
  --title "MiniLM int8 model v1" --notes "sha256 $(sha256sum models/minilm/model.onnx | cut -d' ' -f1)"
```
On the **Pi**, use this model instead of running `prepare-model` there:
```bash
mkdir -p ~/.antiseo/models/minilm && cd ~/.antiseo/models/minilm
gh release download model-v1 --repo Incorrectone/searchbox --clobber
sha256sum model.onnx     # must match the release notes
```
If the Pi already crawled with a different model, the crawl stops with "Model mismatch". Empty the index first: `DELETE FROM documents; DELETE FROM meta WHERE key='embedding_model';` as `pgadmin`.

### 3. Database: images table
From the Pi (where Postgres is reachable):
```bash
source ~/.antiseo/db.env
psql "host=$PGHOST dbname=antiseo user=pgadmin sslmode=require" -v ON_ERROR_STOP=1 -f ~/searchbox/db/002_images.sql
```

### 4. Upstash Redis
Create a database at console.upstash.com: **Region `ap-southeast-1` (Singapore)**, TLS on, eviction on. Copy the `rediss://default:...@....upstash.io:6379` URL.

### 5. Codespaces secrets (repo Settings → Secrets → Codespaces)
| Secret | Value |
|---|---|
| `TF_VAR_TAILSCALE_AUTHKEY_EDGE` | (already set) single-use, pre-approved, `tag:edge` |
| `TF_VAR_ORIGIN_CERT_PEM` | the Cloudflare Origin CA certificate (whole PEM, including BEGIN/END lines) |
| `TF_VAR_ORIGIN_KEY_PEM` | its private key |

GitHub stores secret names upper-case, so the Terraform variables are upper-case too (`var.ORIGIN_CERT_PEM`). Restart the Codespace after adding secrets.

### 6. Terraform: storage + edge
Copy `storage.tf`, `edge.tf`, `edge/` and `cloud-init/edge.yaml` into `infra/azure/`, then:
```bash
cd infra/azure
terraform init -upgrade          # storage.tf uses the random provider (already in versions.tf)
terraform plan -out p2.tfplan    # expect: storage account, container, public IP, NIC, VM, random_string
terraform apply p2.tfplan
terraform output edge_public_ip
terraform output blob_base_url
terraform output -raw crawler_blob_sas; echo
```
The public IP costs about $3.60/month; everything else is in the free tier.

### 7. Cloudflare
- DNS: `A  search  → <edge_public_ip>`, **proxied** (orange cloud).
- SSL/TLS: **Full (strict)**.
- Rate limiting rule: `host eq "search.theincorrect.one" and starts_with(http.request.uri.path, "/api/")` → 30 requests / 10 s per IP → block.
- Configuration Rule for that host: Security Level → Essentially Off.

### 8. App VM secrets
```bash
ssh azureuser@antiseo-app 'sudo mkdir -p /etc/searchbox && sudo nano /etc/searchbox/api.env'
```
Fill it from [`deploy/api.env.example`](deploy/api.env.example):
- the `api` DB password (`API_PW` from the Pi's `~/.antiseo/db.env`);
- the Postgres FQDN (`terraform output -raw pg_fqdn`);
- the Upstash URL;
- your AI Studio key.

Check the exact model IDs in AI Studio and set them if they differ from `appsettings.json`.

### 9. Deploy the API
```bash
api/deploy/deploy.sh models/minilm
```
This builds for linux-arm64 (self-contained), uploads over Tailscale SSH, installs into `/opt/searchbox/releases/<timestamp>`, and switches the `current` symlink only after `/api/v1/health` answers. Otherwise it rolls back to the previous release.

### 10. Crawler: turn on thumbnails
Add to `~/.antiseo/crawler.env` on the Pi:
```
BLOB_BASE_URL=<terraform output blob_base_url>
BLOB_SAS=<terraform output -raw crawler_blob_sas>
```
Then pull, rebuild and crawl:
```bash
cd ~/searchbox && git pull
docker build -t antiseo-crawler crawler/
crawler/deploy/run.sh crawl
```
The crawl summary line now reports `N thumbnails`.

---

## Verify (Phase 2 exit tests)

| Check | How | Expected |
|---|---|---|
| Site works | Phone on mobile data → https://search.theincorrect.one | Search page; results with thumbnails |
| Health | `curl https://search.theincorrect.one/api/v1/health` | `"status":"ok"`, `"redis":"ok"` |
| Rate limit | `for i in $(seq 12); do curl -s -o /dev/null -w "%{http_code} " "https://search.theincorrect.one/api/v1/search?q=t$i"; done` | ten `200`, then `429` |
| Cache | Same query 3 times | `"cached": true` from the 3rd time |
| Redis outage | Put a wrong password in `SearchBox__RedisUrl`, restart | Search still works; health shows `"redis":"down"` |
| AI fallback | Set the first model's `PerDay` to 0 | Summary comes from Gemma 4 |
| Thumbnails private | `curl -I https://<account>.blob.core.windows.net/thumbs/<path>` (no SAS) | `404`/`409`: not public |
| Edge isolation | Azure portal → Network Watcher → IP flow verify on `edge-nic`: TCP out to `10.20.3.4:5432` | **Denied** (rule `alldenyout`) |
| Fallback page | `ssh azureuser@antiseo-app sudo systemctl stop searchbox-api`, reload site | "The search node is offline" page; `/api/*` returns JSON 503 |

## Day-to-day
- **Redeploy the API:** `api/deploy/deploy.sh models/minilm`
- **Change the Caddy config:** edit `infra/azure/edge/Caddyfile.tftpl`, then `infra/azure/edge/deploy.sh` (it validates before reloading)
- **Logs:** `ssh azureuser@antiseo-app journalctl -u searchbox-api -f` · `ssh azureuser@antiseo-edge sudo tail -f /var/log/caddy/access.json`
- **Run locally:** set `SearchBox__Postgres` and `SearchBox__ModelDir`, then `dotnet run --project SearchBox.Api` (Redis and Gemini are optional)
