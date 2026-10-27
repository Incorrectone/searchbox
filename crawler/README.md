# AntiSEO crawler (runs on the Pi)

Weekly job: blog feeds → new posts → clean text → quality gate → MiniLM embedding → Postgres.
It only makes outbound connections. Nothing connects to the Pi.

## What's where

| Path | Purpose |
|---|---|
| `antiseo_crawler/crawl.py` | the pipeline |
| `antiseo_crawler/quality.py` | **the anti-SEO quality gate: yours to write** (keeps everything until then) |
| `antiseo_crawler/net.py` | HTTP client, robots.txt, per-site delay, **home-network guard** |
| `antiseo_crawler/feeds.py`, `extract.py` | feed discovery + conditional GET, trafilatura text extraction |
| `antiseo_crawler/embedder.py` | ONNX MiniLM embeddings (no PyTorch) |
| `antiseo_crawler/db.py` | upserts, `index_version`, model-hash guard |
| `antiseo_crawler/search.py` | CLI hybrid search (same RRF query the API will use) |
| `antiseo_crawler/seed.py` | add domains (Kagi Small Web sample or your list) |
| `antiseo_crawler/prepare_model.py` | one-time model download + int8 quantization |
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
No quotes in this file; Docker's env-file format takes values literally.

**3. Build the image** (5–15 min on a Pi)
```bash
docker build -t antiseo-crawler crawler/
```

**4. Prepare the model** (downloads ~90 MB, writes ~23 MB int8 model)
```bash
crawler/deploy/run.sh prepare-model
```
Check the sanity line: `similar` must be clearly higher than `unrelated`.
**Save the printed sha256**: the API on the app VM must use this exact `model.onnx`
(in `~/.antiseo/models/minilm/`). Never re-run with `--force` once you have data.

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
| `connection timeout` to Postgres inside the container, but `psql` on the Pi works | Docker's bridge network isn't reaching the Tailscale route. Add `--network host \` to the `docker run` in `deploy/run.sh` (the container then shares the Pi's network; the home-network guard still applies to everything it fetches). |
| Every blog says `blocked (... resolves to non-public ...)` | DNS inside the container is returning private addresses (e.g. a Pi-hole). Check `docker run --rm antiseo-crawler ...` can resolve public names. Never "fix" this with `ALLOW_PRIVATE_ADDRESSES=1`. |
| `Model mismatch` | The DB was built with a different model.onnx. Use the original file, or empty `documents` and delete the `embedding_model` row in `meta` to rebuild. |
| `missing .../crawler.env` | Step 2. |
| A blog shows `backed off (403 ...)` | It's blocking or rate limiting bots. Expected for some; the crawler leaves it alone until next week. |

## The quality gate (your part)

`antiseo_crawler/quality.py` explains the input, the suggested signals and the
tuning loop. Change the file, rebuild the image (`docker build ...`), crawl,
run `search --bench`, repeat. Dropped posts print as `DROP <url> score=... reasons`.
