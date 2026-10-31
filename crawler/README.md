# AntiSEO crawler (runs on the Pi)

Weekly job: blog feeds → new posts → clean text → quality gate → MiniLM embedding → Postgres.
It only makes outbound connections. Nothing connects to the Pi.

## What's where

| Path | Purpose |
|---|---|
| `antiseo_crawler/crawl.py` | the pipeline |
| `antiseo_crawler/quality.py` | the anti-SEO quality gate (ads/affiliate links, SEO titles, LLM boilerplate, posting bursts); tests in `tests/` |
| `antiseo_crawler/net.py` | HTTP client, robots.txt, per-site delay, **home-network guard** |
| `antiseo_crawler/feeds.py`, `extract.py` | feed discovery + conditional GET, trafilatura text extraction |
| `antiseo_crawler/embedder.py` | ONNX MiniLM embeddings (no PyTorch) |
| `antiseo_crawler/db.py` | upserts, `index_version`, model-hash guard |
| `antiseo_crawler/search.py` | CLI hybrid search (same RRF query the API will use) |
| `antiseo_crawler/seed.py` | add domains (Kagi Small Web sample or your list) |
| `antiseo_crawler/images.py` | one thumbnail per post: pick, sanitize (re-encode to WebP), upload to Azure Blob |
| `antiseo_crawler/thumbs.py` | backfill: thumbnails for posts indexed before Blob was set up (`run.sh thumbs`) |
| `antiseo_crawler/prepare_model.py` | one-time model download + int8 quantization (done in the Codespace, see below) |
| `deploy/run.sh` | runs any command in the locked-down container |
| `deploy/antiseo-crawl.{service,timer}` | Sunday 03:00 IST schedule |
| `data/queries.txt` | your 10 test queries (Phase 1 exit test) |
| `data/blogs.txt` | blogs you want indexed for sure |

## Setup on the Pi (once)

All commands from the repo root (`~/searchbox`) unless noted.

**1. Check the OS is 64-bit and install Docker**
```bash
dpkg --print-architecture          # must say arm64
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # then log out and back in
```

**2. Create the container's env file** (from the db.env you made earlier)
```bash
source ~/.antiseo/db.env
cat > ~/.antiseo/crawler.env <<EOF
PGHOST=$PGHOST
PGPORT=5432
PGDATABASE=antiseo
PGUSER=crawler
PGPASSWORD=$CRAWLER_PW
PGSSLMODE=require
EOF
chmod 600 ~/.antiseo/crawler.env
```
Optional: add `HC_URL=https://hc-ping.com/<your-check-uuid>` for Healthchecks.io.
Phase 2: add `BLOB_BASE_URL` and `BLOB_SAS` (from `terraform output`) to turn on thumbnails; without them, images are skipped.
No quotes, `export` or spaces in this file; Docker's env-file format takes values literally. Write it with `<<'EOF'` (quoted) and type the values in, so the shell can't replace them with empty variables.

**3. Build the image** (5–15 min on a Pi)
```bash
docker build -t antiseo-crawler crawler/
```

**4. Get the model** (the same file the API uses; it is made once in the Codespace, see `api/README.md` step 2)
```bash
mkdir -p ~/.antiseo/models/minilm && cd ~/.antiseo/models/minilm
gh release download model-v1 --repo Incorrectone/searchbox --clobber
sha256sum model.onnx     # must match the release notes
cd ~/searchbox
```
(Fallback: `crawler/deploy/run.sh prepare-model` builds it on the Pi, but then you must copy that exact file to the API.)

**5. Add blogs**
```bash
crawler/deploy/run.sh seed --kagi 50
crawler/deploy/run.sh seed --file /data/blogs.txt     # if you added any
```

**6. First crawl: small, then everything**
```bash
crawler/deploy/run.sh crawl --limit 5
crawler/deploy/run.sh crawl
```
One line per blog: status, `+N` new posts, and why posts were skipped.

**7. The exit test**: edit `crawler/data/queries.txt` with your 10 queries, then
```bash
crawler/deploy/run.sh search --bench /data/queries.txt
crawler/deploy/run.sh search "one query" -k 10
```

**8. Schedule it**
```bash
# edit User= and paths in the .service file if yours differ
sudo cp crawler/deploy/antiseo-crawl.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now antiseo-crawl.timer
systemctl list-timers antiseo-crawl.timer        # shows the next run
journalctl -u antiseo-crawl -n 50                # logs of the last run
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `connection timeout` to Postgres inside the container, but `psql` on the Pi works | Check `PG_IP` matches the IP in `/etc/hosts`, and that `deploy/run.sh` still has `--network host` (Docker's bridge network doesn't reach the Tailscale route). |
| Every blog says `blocked (... resolves to non-public ...)` | DNS inside the container is returning private addresses (e.g. a Pi-hole). Check `docker run --rm antiseo-crawler ...` can resolve public names. Never "fix" this with `ALLOW_PRIVATE_ADDRESSES=1`. |
| `N thumbnails` is always 0 | `BLOB_BASE_URL`/`BLOB_SAS` missing or expired, or `db/002_images.sql` not applied. Posts indexed before the Blob settings existed need `crawler/deploy/run.sh thumbs` once. |
| `could not translate host name ...postgres.database.azure.com` | Postgres has private DNS only. Put `<private IP> <FQDN>` in the Pi's `/etc/hosts`; for the container, `run.sh` adds it via `--add-host` (set `PG_IP` if it isn't `10.20.3.4`). |
| `local user with ID 1000 does not exist` | `PGUSER` is empty in `crawler.env`. |
| `Model mismatch` | The DB was built with a different model.onnx. Use the original file, or empty `documents` and delete the `embedding_model` row in `meta` to rebuild. |
| `missing .../crawler.env` | Step 2. |
| A blog shows `backed off (403 ...)` | It's blocking or rate limiting bots. Expected for some; the crawler leaves it alone until next week. |

## The quality gate

`antiseo_crawler/quality.py` scores each post (ads and affiliate links, listicle/SEO titles,
LLM boilerplate phrases, posting bursts, a small bonus for author markup) and drops it when
the score falls below `THRESHOLD`. One weak signal alone never drops a post.
Dropped posts print as `DROP <url> score=... reasons`. Tests: `python tests/test_quality.py`.

To tune: crawl, run `search --bench /data/queries.txt`, look at what got in, adjust the lists
or `THRESHOLD`, rebuild the image, repeat.
