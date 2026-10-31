# Terraform, properly: state, variables, modules, config, drift, CI/CD

You already know what Terraform *does*: describe infrastructure in `.tf` files, run `plan` and `apply`. This guide explains the machinery around that, using this repo (`infra/azure/`) as the example throughout.

---

## 0. The mental model: three versions of reality

Terraform always juggles three things:

```
   ┌──────────────┐        ┌──────────────┐        ┌──────────────┐
   │ 1. CODE      │        │ 2. STATE     │        │ 3. REALITY   │
   │ *.tf files   │        │ the state    │        │ what actually│
   │ what you WANT│        │ file: what   │        │ exists in    │
   │              │        │ Terraform    │        │ Azure        │
   │              │        │ BELIEVES     │        │              │
   │              │        │ exists       │        │              │
   └──────────────┘        └──────────────┘        └──────────────┘
```

`terraform plan` does this:
1. **Refresh**: ask Azure about every resource listed in the state, and update the state's picture of reality.
2. **Diff**: compare *code* with *refreshed state*.
3. **Print the difference** as `+ create`, `~ update`, `- destroy`, `-/+ replace`.

`terraform apply` makes the API calls that close that difference, then **writes the new state**.

Almost every confusing situation (a lock error, drift, "why does it want to recreate my VM?") makes sense once you ask: *which of the three disagrees with which?*

---

## 1. State files

### What's in it
State is a JSON file. For every resource Terraform manages, it records the **mapping from your code name to the real object**, plus every attribute Azure reported:

```json
{
  "type": "azurerm_linux_virtual_machine",
  "name": "appvm",
  "instances": [{
    "attributes": {
      "id": "/subscriptions/ae01.../resourceGroups/rg-antiseo/providers/Microsoft.Compute/virtualMachines/app-vm",
      "size": "Standard_B2pts_v2",
      "private_ip_address": "10.20.2.4",
      "custom_data": "I2Nsb3VkLWNvbmZpZw...",
      ...
    }
  }]
}
```

The key line is the mapping: `azurerm_linux_virtual_machine.appvm` ↔ that Azure resource ID. Without it, Terraform can't know that the VM in Azure is "its" `appvm`.

### Why it exists at all
- **Mapping:** code says `appvm`; Azure says `/subscriptions/.../app-vm`. State links them.
- **Deletions:** if you delete a resource block from the code, Terraform only knows to *destroy* it because the state still lists it.
- **Dependencies and speed:** the plan uses stored attributes (like the NIC's ID) instead of re-deriving everything.
- **Outputs:** `terraform output pg_fqdn` reads the state, not Azure.

### Where yours lives: a remote backend
```hcl
backend "azurerm" {
  resource_group_name  = "rg-tfstate"
  storage_account_name = "tfstateantiseo11401"
  container_name       = "tfstate"
  key                  = "azure.tfstate"   # the blob name
  use_azuread_auth     = true
}
```

| Local state (`terraform.tfstate` next to your code) | Remote state (yours, in Azure Blob) |
|---|---|
| Lost when the Codespace is deleted | Survives anything; can be versioned |
| Only one machine can use it | Codespace, CI and teammates share one truth |
| No locking | **Locking** (below) |
| Secrets sit on your disk | Secrets sit in a private storage account |

### Locking (the error you hit)
Before `plan` or `apply`, Terraform takes a **lease on the state blob**. A second run waits or fails with `state blob is already locked`. This prevents two applies at once, each overwriting the other's changes and corrupting the state.

Your lock was left behind by a `plan` stuck at an `Enter a value` prompt. `terraform force-unlock <ID>` removes a stale lock. **Only do this when you're sure nothing else is running.**

### State contains secrets
Everything is stored in plain JSON: the Postgres admin password (`random_password.pg_admin`), the SAS tokens, the Tailscale auth key inside `custom_data`. `sensitive = true` only hides values in **terminal output**; they're still in the state. So:
- treat the state storage account like a password vault (yours is private, Entra-ID authenticated);
- never commit `*.tfstate` (your `.gitignore` already blocks it);
- the same goes for saved plan files (`*.tfplan`): they contain secrets too.

### Never edit state by hand. Use these commands
| Command | Use it when |
|---|---|
| `terraform state list` | see everything Terraform manages |
| `terraform state show azurerm_linux_virtual_machine.appvm` | see one resource's recorded attributes |
| `terraform state mv A B` | you **renamed** a resource in code and don't want it destroyed and recreated (better: a `moved` block, §3) |
| `terraform state rm A` | stop managing something **without deleting it** in Azure |
| `terraform import A <azure-id>` or an `import` block | adopt something created by hand (e.g. the `rg-tfstate` group) |
| `terraform plan -refresh-only` | see only what changed in reality, without proposing code changes (§5) |

Example: if you ever wanted Terraform to manage `rg-tfstate`:
```hcl
import {
  to = azurerm_resource_group.tfstate
  id = "/subscriptions/ae01e540-.../resourceGroups/rg-tfstate"
}
resource "azurerm_resource_group" "tfstate" {
  name     = "rg-tfstate"
  location = "centralindia"
}
```
`plan` then says `import` instead of `create`.

---

## 2. Variables, locals, outputs, data sources

Four kinds of value, four jobs:

| | Purpose | In this repo |
|---|---|---|
| **`variable`** | inputs from **outside** the code | `TAILSCALE_AUTHKEY_APP`, `ORIGIN_CERT_PEM`, `location`, `site_host` |
| **`locals`** | named values computed **inside** the code | `local.caddyfile`, `local.sas_start` |
| **`output`** | values exposed **to you or other tools** after apply | `pg_fqdn`, `edge_public_ip`, `crawler_blob_sas` |
| **`data`** | **read** something that exists, without managing it | `data.azurerm_storage_account_blob_container_sas.edge_read` |

### Input variables in detail
```hcl
variable "site_host" {
  type        = string
  default     = "search.theincorrect.one"     # optional; no default = must be provided
  description = "Public hostname"
  validation {
    condition     = can(regex("^[a-z0-9.-]+$", var.site_host))
    error_message = "site_host must be a plain hostname."
  }
}

variable "ORIGIN_KEY_PEM" {
  type      = string
  sensitive = true      # hidden in plan/apply output (still in state!)
}
```
Types can be structured too: `list(string)`, `map(number)`, `object({ size = string, disk_gb = number })`.

### Where variable values come from (highest priority wins)
1. `-var 'site_host=x'` / `-var-file=prod.tfvars` on the command line
2. `*.auto.tfvars` files (alphabetical order)
3. `terraform.tfvars`
4. **environment variables `TF_VAR_<name>`** ← what your Codespaces secrets use
5. the `default` in the `variable` block
6. otherwise Terraform **prompts** (`Enter a value:`), which is what happened when the secret was missing

`TF_VAR_` matching is case-sensitive. GitHub upper-cases secret names, which is why your variables are upper-case.

**`.tfvars` files** are just name = value lists:
```hcl
# prod.tfvars
site_host = "search.theincorrect.one"
location  = "eastasia"
```
Commit tfvars files that hold *settings*; never commit ones that hold *secrets* (your `.gitignore` blocks `*.tfvars` to be safe).

### Locals
```hcl
locals {
  caddyfile = templatefile("${path.module}/edge/Caddyfile.tftpl", { ... })
}
```
Use them to avoid repeating an expression, or to give a meaningful name to something long. Unlike variables, nobody outside can set them.

### Outputs
```hcl
output "crawler_blob_sas" {
  value     = trimprefix(data....sas, "?")
  sensitive = true
}
```
`terraform output` lists them, and `terraform output -raw crawler_blob_sas` prints one raw value, for use in scripts (as `edge/deploy.sh` does). Outputs are also how **modules** pass values back to their caller (§3).

---

## 3. Reusable modules

### What a module is
**Every folder of `.tf` files is a module.** `infra/azure/` is your *root module*, the one you run commands in. A *child module* is another folder that the root calls, like a function:

```
infra/
  azure/                    ← root module (has the backend, calls modules)
    main.tf
  modules/
    linux_vm/               ← child module: a reusable "VM + NIC" recipe
      variables.tf          ← its parameters
      main.tf               ← its resources
      outputs.tf            ← its return values
```

| Function concept | Module equivalent |
|---|---|
| parameters | `variable` blocks |
| body | resources |
| return values | `output` blocks |
| calling it | a `module` block |

### Example: your two VMs are nearly identical
`app.tf` and `edge.tf` both define a NIC plus a Linux VM with a P6 disk, Ubuntu 24.04, boot diagnostics and `ignore_changes = [custom_data]`. As a module:

```hcl
# modules/linux_vm/variables.tf
variable "name"        { type = string }
variable "size"        { type = string }
variable "subnet_id"   { type = string }
variable "private_ip"  { type = string }
variable "image_sku"   { type = string }            # "server" or "server-arm64"
variable "custom_data" { type = string, sensitive = true }
variable "public_ip_id" {
  type    = string
  default = null                                     # optional parameter
}
variable "rg"          { type = object({ name = string, location = string }) }
variable "ssh_public_key" { type = string }
```
```hcl
# modules/linux_vm/main.tf
resource "azurerm_network_interface" "this" {
  name                = "${var.name}-nic"
  location            = var.rg.location
  resource_group_name = var.rg.name
  ip_configuration {
    name                          = "internal"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Static"
    private_ip_address            = var.private_ip
    public_ip_address_id          = var.public_ip_id
  }
}

resource "azurerm_linux_virtual_machine" "this" {
  name                            = "${var.name}-vm"
  # ... size, image (var.image_sku), P6 disk, boot_diagnostics, custom_data ...
  network_interface_ids           = [azurerm_network_interface.this.id]
  lifecycle { ignore_changes = [custom_data] }
}
```
```hcl
# modules/linux_vm/outputs.tf
output "private_ip" { value = azurerm_network_interface.this.private_ip_address }
output "vm_id"      { value = azurerm_linux_virtual_machine.this.id }
```
And the root module calls it twice:
```hcl
module "app" {
  source      = "../modules/linux_vm"
  name        = "app"
  size        = "Standard_B2pts_v2"
  image_sku   = "server-arm64"
  subnet_id   = azurerm_subnet.snetapp.id
  private_ip  = "10.20.2.4"
  custom_data = base64encode(templatefile(...))
  rg          = azurerm_resource_group.resourcegrp
  ssh_public_key = file("${path.module}/keys/antiseo.pub")
}

module "edge" {
  source       = "../modules/linux_vm"
  name         = "edge"
  size         = "Standard_B2ats_v2"
  image_sku    = "server"
  subnet_id    = azurerm_subnet.snetedge.id
  private_ip   = "10.20.1.4"
  public_ip_id = azurerm_public_ip.edge.id
  # ...
}

# use a module's output elsewhere:
#   app_ip = module.app.private_ip
```
A fix to the VM recipe (say, a hardening setting) is then made once and applies to both.

### Refactoring into a module without destroying anything
Moving a resource into a module changes its **address**: `azurerm_linux_virtual_machine.appvm` becomes `module.app.azurerm_linux_virtual_machine.this`. Terraform would see "old address gone, new address appeared" and plan **destroy + create**. Tell it they're the same object:
```hcl
moved {
  from = azurerm_linux_virtual_machine.appvm
  to   = module.app.azurerm_linux_virtual_machine.this
}
moved {
  from = azurerm_network_interface.appvmnic
  to   = module.app.azurerm_network_interface.this
}
```
Now `plan` shows `has moved to` and **0 to destroy**. Always check that before applying a refactor.

### Module sources
```hcl
source = "../modules/linux_vm"                          # local folder
source = "Azure/avm-res-network-virtualnetwork/azurerm"  # Terraform Registry
version = "~> 0.4"                                       # pin registry modules!
source = "git::https://github.com/org/repo.git//modules/vm?ref=v1.2.0"  # git, pinned to a tag
```
Run `terraform init` after adding or changing a module source; that's when modules are downloaded into `.terraform/modules/`.

### When a module is worth it
- **Yes:** the same pattern 2+ times (your two VMs), or something you'd reuse across projects.
- **No:** wrapping a single resource just to rename its arguments. That adds indirection and no reuse. Many good codebases are a flat root module, like yours is now.

---

## 4. Config files, and local vs cloud

### The files in a Terraform folder
| File / folder | What it is | Commit? |
|---|---|---|
| `*.tf` | your code. **All `.tf` files in a folder are merged into one config**; file names are only for humans | ✅ |
| `versions.tf` (any name works) | the `terraform {}` block: required Terraform version, providers, backend | ✅ |
| `.terraform.lock.hcl` | exact provider versions + checksums chosen by `init` (like `package-lock.json`) | ✅ so CI and Codespaces get identical providers |
| `.terraform/` | downloaded providers and modules, plus backend settings (cache) | ❌ |
| `terraform.tfstate`, `*.tfstate.backup` | **local** state (you don't have any: yours is remote) | ❌ never |
| `*.tfvars` | variable values | settings ✅ / secrets ❌ |
| `*.tfplan` | saved plans (contain secrets) | ❌ |
| `*.tftpl` | templates read by `templatefile()` (your `Caddyfile.tftpl`) | ✅ |

### What `terraform init` does
1. Reads the `terraform {}` block.
2. Downloads providers (`azurerm`, `random`) into `.terraform/`, respecting the lock file.
3. Downloads modules.
4. **Connects to the backend**, here your Azure storage account.

Rerun it after changing providers, modules or the backend. `init -upgrade` lets it move to newer provider versions allowed by your constraints (`~> 4.0`) and updates the lock file.

### Local → cloud: what moves where
| Thing | Local (laptop/Codespace) | Cloud |
|---|---|---|
| Code (`.tf`) | yes | in **Git**, the source of truth |
| State | ❌ (would be lost / unshared) | **backend** (your Azure Blob container) |
| Provider credentials | env vars (`ARM_CLIENT_ID`, …) | CI secrets or OIDC (§6) |
| Variable values | `TF_VAR_*` env vars / tfvars | CI secrets / committed tfvars |

**Moving existing local state to a remote backend:** add the `backend` block, run `terraform init`, and it asks *"Do you want to copy existing state to the new backend?"*. Answer yes, and it uploads the state. You started remote from day one, so you never needed this.

**Partial backend config:** backend blocks can't use variables, so names often differ per environment. You can leave fields out of the code and pass them at init:
```bash
terraform init -backend-config="key=prod.tfstate"
```
That's how one codebase can have separate `dev` and `prod` states.

### Several environments
- **Simple and explicit (recommended):** a folder per environment (`envs/dev`, `envs/prod`), each a small root module with its own backend `key` and tfvars, both calling shared modules.
- **Workspaces** (`terraform workspace new dev`): same folder, separate state per workspace. Handy, but it's easy to apply to the wrong one because the code looks identical.

---

## 5. Configuration drift

**Drift = reality changed without Terraform.** For example:
- someone resizes the app VM in the Azure portal;
- an NSG rule is added by hand "just to test";
- Azure itself changes something (a platform default, or an availability zone it picked).

### How drift shows up
```bash
terraform plan
```
The refresh step finds the difference, and the plan proposes to put reality **back to what the code says**:
```
  ~ resource "azurerm_linux_virtual_machine" "appvm" {
      ~ size = "Standard_B2ps_v2" -> "Standard_B2pts_v2"   # someone changed it; code will revert it
```
To see *only* what drifted, without any proposal from your code changes:
```bash
terraform plan -refresh-only      # "Objects have changed outside of Terraform"
terraform apply -refresh-only     # accept reality into the state, change nothing in Azure
```

### Three ways to resolve it
| Situation | Do this |
|---|---|
| The manual change was a mistake | `terraform apply`: Terraform **reverts** it |
| The manual change was right | **Update the code** to match, so the plan shows no changes. Code stays the source of truth |
| Azure or another system legitimately owns the value | `lifecycle { ignore_changes = [attr] }`: Terraform stops comparing it |

You already use the third one twice:
- `postgres.tf`: `ignore_changes = [zone]`, because **Azure picks** the availability zone;
- `app.tf` / `edge.tf`: `ignore_changes = [custom_data]`, because cloud-init only runs on first boot, and you don't want a new Tailscale key to **rebuild** the VM.

### Preventing drift
- Do changes through code; treat the portal as read-only for anything Terraform owns.
- Run a **scheduled drift check** (§6), so drift is found in hours, not months.
- Optional: an Azure **resource lock** (`CanNotDelete`) on critical resources.

### A related trap: "forces replacement"
Some attributes can't be changed in place: renaming a VM, or changing a subnet's address range. The plan shows:
```
-/+ resource "azurerm_linux_virtual_machine" "appvm" {
      ~ name = "app-vm" -> "app-server" # forces replacement
```
`-/+` means **destroy, then create**. Read every plan for `-/+` and `destroy` before you type `yes`.

---

## 6. Terraform in CI/CD (GitHub Actions)

### Why
- Every infrastructure change becomes a **pull request** with the plan visible for review.
- Apply happens from one controlled place, not "whatever was in someone's terminal".
- Credentials live in GitHub, not on laptops.
- Drift checks can run on a schedule.

### The standard flow
```
 feature branch ──push──▶ PR opened
                           │
                           ▼
              ┌─────────────────────────┐
              │ CI: fmt -check          │
              │     validate            │   fails fast on style/syntax
              │     plan                │   plan shown in the PR for review
              └─────────────────────────┘
                           │  review + merge
                           ▼
              ┌─────────────────────────┐
 main ───────▶│ CD: plan again          │
              │     wait for approval   │   GitHub "environment" with required reviewer
              │     apply               │
              └─────────────────────────┘
 nightly ────▶  plan -detailed-exitcode     drift check: exit code 2 = something drifted
```

### Authentication: OIDC instead of a stored secret
Right now your service principal uses a **client secret** (`ARM_CLIENT_SECRET`): a long-lived password. In CI, the better way is **OIDC federation**:
1. GitHub gives each workflow run a short-lived signed token saying "this is repo Incorrectone/searchbox, branch main".
2. You tell Azure to trust tokens with exactly that identity (a **federated credential** on the app registration).
3. Terraform exchanges the token for an Azure access token. No secret is stored anywhere.

One-time setup (Cloud Shell):
```bash
az ad app federated-credential create --id 5d56f3f5-200b-44b8-b330-9e5a57a28ef5 --parameters '{
  "name": "github-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:Incorrectone/searchbox:environment:production",
  "audiences": ["api://AzureADTokenExchange"]
}'
# and one more with subject "repo:Incorrectone/searchbox:pull_request" for PR plans
```
Then the workflow sets `ARM_USE_OIDC=true` along with the client, tenant and subscription IDs. Those IDs aren't secret; the trust relationship replaces the password.

### Example workflow: `.github/workflows/terraform.yml`
```yaml
name: terraform

on:
  pull_request:
    paths: ["infra/azure/**"]
  push:
    branches: [main]
    paths: ["infra/azure/**"]
  schedule:
    - cron: "30 2 * * *"          # nightly drift check (08:00 IST)

permissions:
  id-token: write                 # needed for OIDC
  contents: read
  pull-requests: write            # to comment the plan on PRs

env:
  ARM_CLIENT_ID: ${{ vars.ARM_CLIENT_ID }}
  ARM_TENANT_ID: ${{ vars.ARM_TENANT_ID }}
  ARM_SUBSCRIPTION_ID: ${{ vars.ARM_SUBSCRIPTION_ID }}
  ARM_USE_OIDC: "true"
  TF_IN_AUTOMATION: "true"
  # secrets the config needs as variables:
  TF_VAR_TAILSCALE_AUTHKEY_APP: ${{ secrets.TF_VAR_TAILSCALE_AUTHKEY_APP }}
  TF_VAR_TAILSCALE_AUTHKEY_EDGE: ${{ secrets.TF_VAR_TAILSCALE_AUTHKEY_EDGE }}
  TF_VAR_ORIGIN_CERT_PEM: ${{ secrets.TF_VAR_ORIGIN_CERT_PEM }}
  TF_VAR_ORIGIN_KEY_PEM: ${{ secrets.TF_VAR_ORIGIN_KEY_PEM }}

defaults:
  run:
    working-directory: infra/azure

concurrency:
  group: terraform-azure          # never two runs at once (state lock would block anyway)
  cancel-in-progress: false

jobs:
  plan:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform fmt -check -recursive
      - run: terraform init -input=false
      - run: terraform validate
      - id: plan
        run: terraform plan -input=false -no-color -lock-timeout=5m | tee plan.txt
      - uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const plan = fs.readFileSync('infra/azure/plan.txt', 'utf8').slice(-60000);
            await github.rest.issues.createComment({
              owner: context.repo.owner, repo: context.repo.repo,
              issue_number: context.issue.number,
              body: "### Terraform plan\n```\n" + plan + "\n```"
            });

  apply:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    environment: production       # set "Required reviewers" on this environment = manual approval gate
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init -input=false
      - run: terraform plan -input=false -lock-timeout=5m -out=ci.tfplan
      - run: terraform apply -input=false ci.tfplan   # applies exactly the plan just made

  drift:
    if: github.event_name == 'schedule'
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform init -input=false
      - name: Detect drift
        run: |
          set +e
          terraform plan -input=false -detailed-exitcode -lock-timeout=5m -no-color > drift.txt
          code=$?
          if [ $code -eq 2 ]; then echo "::warning::Infrastructure drift detected"; cat drift.txt; exit 1; fi
          exit $code
```

Key details in that file:
- **`-input=false`**: never wait for a prompt. CI has no keyboard; a missing variable should fail immediately, not hang (the lock problem you saw locally).
- **`-out=ci.tfplan` then `apply ci.tfplan`**: applies *exactly* what was planned, even if code or reality changed in between.
- **`-detailed-exitcode`**: exit 0 = no changes, 2 = changes (drift), 1 = error. That's what makes the drift job possible.
- **`environment: production` with required reviewers**: GitHub pauses the apply job until you click *Approve*. That's your manual safety gate.
- **`concurrency`** and **`-lock-timeout`**: a second run waits rather than failing on the lock.
- **Plan output contains values.** Keep secrets marked `sensitive` so they show as `(sensitive value)` in the PR comment. Don't upload `.tfplan` files as artifacts in public repos.
- `vars.*` are non-secret repository *variables*; `secrets.*` are encrypted secrets. Both are set in repo Settings → Secrets and variables → **Actions** (separate from the Codespaces secrets you already have).

### Would this work for your project today?
Mostly. Two things to note:
- Your Tailscale auth keys are **single-use**. The VMs already exist and `custom_data` is ignored after creation, so CI only needs *a* value for the variable, not a fresh key, unless it's recreating a VM.
- The state storage account must allow the CI identity: give the service principal the **Storage Blob Data Contributor** role on it (you already did for the Codespace).

---

## 7. Daily commands, cheat sheet

```bash
terraform fmt -recursive            # format all files
terraform validate                  # syntax + internal consistency (no Azure calls)
terraform plan -out=x.tfplan        # see and save the change set
terraform apply x.tfplan            # apply exactly that plan
terraform plan -refresh-only        # what drifted?
terraform output                    # list outputs  (-raw NAME for one value)
terraform state list                # what's managed
terraform state show ADDRESS        # one resource's recorded attributes
terraform console                   # try expressions: cidrsubnet("10.20.0.0/16", 8, 3)
terraform graph | dot -Tsvg > g.svg # dependency graph picture
terraform force-unlock LOCK_ID      # remove a stale lock (only if nothing is running!)
```

## 8. Mistakes to avoid (several already met in this project)
1. **Running with missing variables** → it prompts and holds the lock. Fix the env vars; in CI use `-input=false`.
2. **Renaming a resource** without a `moved` block → destroy and recreate.
3. **Applying without reading** the plan for `-/+` or `destroy`.
4. **Committing state, plans or secret tfvars.**
5. **Portal edits to Terraform-owned resources** → drift. Change the code instead.
6. **Unpinned providers or modules** → a new version changes behaviour silently. Commit `.terraform.lock.hcl`; pin module versions.
7. **One giant state for everything** → slow plans and a big blast radius. Split by lifecycle (network vs apps vs data) once it grows. Not needed at this project's size.
