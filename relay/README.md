# Gemini relay (Cloud Run, Mumbai)

The app VM is in Azure **East Asia, which is Hong Kong**, and the Gemini API refuses requests from Hong Kong:
```
400 FAILED_PRECONDITION: User location is not supported for the API use.
```
This relay runs on Cloud Run in **asia-south1 (Mumbai)**. The API sends its summary calls here, and the relay forwards them to Gemini from an Indian Google IP.

```
app VM (Hong Kong) --HTTPS, relay token--> Cloud Run relay (Mumbai) --real key--> Gemini API
```

## What it allows
| Request | Result |
|---|---|
| `POST /v1beta/models/<allowed model>:generateContent`, JSON body ≤ 64 KB, correct token | forwarded; Google's answer is passed back unchanged |
| `GET /v1beta/models`, correct token | Google's model list (for finding model IDs) |
| `GET /health` | `{"ok": true}` when the key, token and model list are all set |
| wrong or missing token | `401` |
| any other model, method, path, or a query string | `403` / `404` / `405`, and never reaches Google |

- **The token:** the API already sends its "API key" in the `x-goog-api-key` header. With the relay, that setting holds the **relay token**, so the API needs no code change. The token is compared in constant time.
- **The real Gemini key** exists only in Secret Manager and the relay's memory. It is no longer on the app VM. A leaked relay token only lets someone make summary calls to two models. It can't be turned into the key.
- **Limits:** one instance at most, 20 requests at a time, 256 MB. That's a hard ceiling on cost and on abuse. The API's own daily quotas still apply in front of it.
- **No dependencies:** Python standard library only. Tests: `python relay/test_relay.py` (17 checks against a fake Gemini). `deploy.sh` runs them before every deploy.

## Setup (once)

### 1. A GCP project with billing linked
Cloud Run needs a billing account linked to the project even when usage stays inside the free tier. This relay stays well inside it: 2M requests a month free, Secret Manager's 6 free secret versions, and free Cloud Build minutes. Note the project ID.

### 2. gcloud in the Codespace
```bash
curl -sSL https://sdk.cloud.google.com | bash -s -- --disable-prompts --install-dir=$HOME
echo 'source $HOME/google-cloud-sdk/path.bash.inc' >> ~/.bashrc && source ~/.bashrc
gcloud auth login --no-launch-browser                    # for gcloud commands
gcloud auth application-default login --no-launch-browser # for Terraform
gcloud config set project <your-project-id>
```
Also add a Codespaces secret `GCP_PROJECT` = your project ID, then reload the Codespace. It becomes `TF_VAR_GCP_PROJECT`, like your other secrets.

### 3. Terraform (`infra/gcp`)
In `infra/gcp/main.tf`, replace the three `CHANGE_ME` values in the `backend` block with the same values as in `infra/azure`. The key is already different (`gcp.tfstate`), so the two states never mix.
```bash
cd infra/gcp
terraform init
terraform plan -out gcp.tfplan      # expect: 4 APIs, 1 service account, 2 secrets, 2 IAM bindings
terraform apply gcp.tfplan
```

### 4. Put the values in the secrets
This uses gcloud, not Terraform, so the key never lands in Terraform state.
```bash
read -rsp "Gemini key: " K; echo; printf %s "$K" | gcloud secrets versions add gemini-api-key --data-file=-; unset K
TOKEN=$(openssl rand -hex 32); echo "relay token: $TOKEN"     # copy it; the API needs it in step 6
printf %s "$TOKEN" | gcloud secrets versions add relay-token --data-file=-
```

### 5. Find the model IDs and deploy
The Codespace isn't in Hong Kong, so you can list models directly with your key:
```bash
read -rsp "Gemini key: " K; echo
curl -s -H "x-goog-api-key: $K" "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200" \
  | grep -oE '"models/(gemini[^"]*flash-lite[^"]*|gemma-4[^"]*)"'; unset K
```
Pick one Flash Lite ID and one Gemma 4 ID, without the `models/` prefix, then:
```bash
cd /workspaces/searchbox
relay/deploy.sh "<flash-lite id>,<gemma-4 id>"
```
The first deploy takes a few minutes, because Cloud Build builds the container. At the end it prints `health: {"ok": true}` and the two lines for step 6.

### 6. Point the API at the relay (on the app VM)
```bash
sudo nano /etc/searchbox/api.env
```
```
SearchBox__Gemini__Endpoint=https://gemini-relay-xxxxx.asia-south1.run.app/v1beta
SearchBox__Gemini__ApiKey=<relay token from step 4>
SearchBox__Gemini__Models__0__Id=<flash-lite id>
SearchBox__Gemini__Models__1__Id=<gemma-4 id>
```
The model IDs must match the ones you deployed with, or the relay answers `403`. Then:
```bash
sudo systemctl restart searchbox-api
```
Search something on the site and click **Summarize with AI**.

## Checks
| Check | Command | Expect |
|---|---|---|
| Health | `curl -s $URL/health` | `{"ok": true}` |
| No token | `curl -s -X POST $URL/v1beta/models/x:generateContent` | `401` |
| Through the relay | `curl -s -H "x-goog-api-key: $TOKEN" -H 'Content-Type: application/json' -d '{"contents":[{"parts":[{"text":"say hi"}]}]}' $URL/v1beta/models/<flash-lite id>:generateContent` | a reply with text |
| From the app VM | the same command, run on the app VM | a reply. This proves the Hong Kong block is gone. |
| Logs | Cloud Console → Cloud Run → gemini-relay → Logs | one line per request: method, path, status. Never the token, key or body. |

## Troubleshooting
| Symptom | Fix |
|---|---|
| Deploy fails with a Cloud Build permission error | Newer projects build as the default compute service account. Grant it the builder role: `gcloud projects add-iam-policy-binding <project> --member serviceAccount:$(gcloud projects describe <project> --format 'value(projectNumber)')-compute@developer.gserviceaccount.com --role roles/run.builder`, then redeploy. |
| `health` shows `{"ok": false}` | A secret has no value yet (step 4), or the model list was empty. |
| The API log says `returned 401` | `SearchBox__Gemini__ApiKey` on the VM isn't the relay token. |
| The API log says `returned 403: model ... is not allowed` | The model IDs in `api.env` don't match `deploy.sh`'s list. |
| Still `User location is not supported` | `SearchBox__Gemini__Endpoint` still points at Google, not the relay. |
| The first summary after a quiet spell is slow | A cold start (about 1–2 s): `min-instances` is 0 to stay free. |

## Rotate or change things
- **New Gemini key:** `printf %s "$K" | gcloud secrets versions add gemini-api-key --data-file=-`, then rerun `relay/deploy.sh "<models>"` so a new instance reads it.
- **New relay token:** the same, for `relay-token`. Then update `api.env` and restart the API.
- **Different models:** rerun `relay/deploy.sh` with the new list, and update `api.env`.
