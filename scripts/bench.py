#!/usr/bin/env python3
"""One-off latency benchmark for the live SearchBox site.

Measures, from wherever you run it:
  * the edge on its own (Cloudflare -> Caddy -> Anubis, never reaches the app VM)
  * /api/v1/health and /api/v1/stats (edge -> app VM -> Postgres/Redis)
  * /api/v1/search and /api/v1/images, split by the response's own `cached`
    flag, so cold (embed + HNSW + tsvector + RRF) and cached runs are reported
    separately, together with the server's own `took_ms`
  * thumbnails (/img/...), split by Cloudflare's cf-cache-status (HIT vs MISS)
  * optionally /api/v1/summary (Gemini via the relay), with --summary

Each request opens a new connection, timed as DNS / TCP / TLS / TTFB / total.
That is the worst case a first-time visitor sees. Keep-alive would be faster.

The API's own rate limits apply to you too (search: burst 10, then 1 every 3 s;
summary: burst 3, then 1 every 20 s), so search calls are paced and any 429 is
waited out and retried. A full run takes about 3-4 minutes.

Standard library only:
    python3 scripts/bench.py                      # default site and queries
    python3 scripts/bench.py --summary            # also time 2 AI summaries
    python3 scripts/bench.py --base https://search.theincorrect.one --rounds 3

Writes bench-results.json (every request) and bench-results.md (the summary
table) to the current directory. Run it from the Codespace and its traffic
stays out of the public dashboard (that network is in siem.excluded_networks).
"""

import argparse
import json
import platform
import socket
import ssl
import statistics
import sys
import time
import urllib.parse
from datetime import datetime, timezone

DEFAULT_QUERIES = [
    "rust memory leak",
    "self hosting a home server",
    "why i left big tech",
    "postgres performance tuning",
    "learning japanese as an adult",
    "building a mechanical keyboard",
    "static site generators",
    "burnout and side projects",
    "linux on old laptops",
    "writing a compiler",
]

UA = "searchbox-bench/1.0 (one-off latency benchmark by the site owner)"


# ---------------------------------------------------------------- timing

def timed_get(url, timeout=30.0, accept="application/json"):
    """GET over a fresh connection. Returns a dict of phase timings in ms."""
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname, u.port or 443
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    r = {"url": url, "error": None}
    t0 = time.perf_counter()
    sock = None
    try:
        addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
        t_dns = time.perf_counter()

        sock = socket.socket(addr[0], addr[1], addr[2])
        sock.settimeout(timeout)
        sock.connect(addr[4])
        t_tcp = time.perf_counter()

        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["http/1.1"])
        sock = ctx.wrap_socket(sock, server_hostname=host)
        t_tls = time.perf_counter()

        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {UA}\r\n"
               f"Accept: {accept}\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n")
        sock.sendall(req.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            if not buf:
                t_first = time.perf_counter()
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
        t_end = time.perf_counter()

        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = dechunk(body)

        ms = lambda a, b: round((b - a) * 1000, 1)
        r.update(status=status, headers=headers, body=body, bytes=len(body),
                 dns_ms=ms(t0, t_dns), tcp_ms=ms(t_dns, t_tcp), tls_ms=ms(t_tcp, t_tls),
                 ttfb_ms=ms(t_tls, t_first), total_ms=ms(t0, t_end),
                 colo=headers.get("cf-ray", "-").rsplit("-", 1)[-1],
                 cf_cache=headers.get("cf-cache-status", "-"))
    except Exception as e:  # report and keep going
        r.update(status=0, error=f"{type(e).__name__}: {e}", total_ms=round((time.perf_counter() - t0) * 1000, 1))
    finally:
        if sock:
            sock.close()
    return r


def dechunk(data):
    out = b""
    while data:
        size_line, _, data = data.partition(b"\r\n")
        size = int(size_line.split(b";")[0] or b"0", 16)
        if size == 0:
            break
        out += data[:size]
        data = data[size + 2:]
    return out


def get_json_paced(url, label, results, max_retries=4):
    """GET a rate-limited endpoint; wait out 429s. Returns (record, json or None)."""
    for _ in range(max_retries + 1):
        r = timed_get(url)
        if r.get("status") == 429:
            wait = int(r["headers"].get("retry-after", "3") or 3) + 0.5
            print(f"    429 rate limited, waiting {wait:.0f}s", flush=True)
            results.append(strip(r, label + " (429)"))
            time.sleep(wait)
            continue
        break
    data = None
    if r.get("status") == 200:
        try:
            data = json.loads(r["body"])
        except ValueError:
            pass
    return r, data


def strip(r, label, **extra):
    """Keep what goes into the JSON file; drop body and most headers."""
    keep = {k: v for k, v in r.items() if k not in ("body", "headers")}
    keep["label"] = label
    keep.update(extra)
    return keep


# ---------------------------------------------------------------- stats

def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def summarize(name, recs, field="total_ms", server=None):
    vals = [x[field] for x in recs if x.get(field) is not None]
    row = {"name": name, "n": len(vals),
           "p50": pct(vals, 50), "p90": pct(vals, 90), "p95": pct(vals, 95),
           "max": max(vals) if vals else None,
           "mean": round(statistics.mean(vals), 1) if vals else None}
    if server:
        sv = [x[server] for x in recs if x.get(server) is not None]
        row["server_p50"] = pct(sv, 50)
        row["server_p95"] = pct(sv, 95)
    return row


# ---------------------------------------------------------------- run

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="https://search.theincorrect.one")
    ap.add_argument("--rounds", type=int, default=3,
                    help="times each query is sent (cache admission needs a 2nd hit, so 3 gives cached samples)")
    ap.add_argument("--pace", type=float, default=3.2, help="seconds between search calls (API refills 1 per 3 s)")
    ap.add_argument("--probes", type=int, default=10, help="samples for the edge / health / stats probes")
    ap.add_argument("--thumbs", type=int, default=20, help="max thumbnails to time")
    ap.add_argument("--summary", action="store_true", help="also time 2 AI summaries (slow, uses Gemini quota)")
    ap.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    ap.add_argument("--out", default="bench-results")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    results = []

    def api(path, **params):
        return f"{base}{path}?{urllib.parse.urlencode(params)}" if params else f"{base}{path}"

    started = datetime.now(timezone.utc)
    print(f"SearchBox benchmark against {base}\n")

    # 1. Edge only: "/" is answered by Anubis (challenge page) on the edge VM.
    print(f"[1/5] edge only: GET / x{a.probes}")
    for _ in range(a.probes):
        r = timed_get(base + "/", accept="text/html")
        results.append(strip(r, "edge /"))
        time.sleep(0.3)

    # 2. Cheap API calls: full path to the app VM.
    print(f"[2/5] API: health and stats x{a.probes}")
    for _ in range(a.probes):
        r = timed_get(api("/api/v1/health"))
        results.append(strip(r, "health"))
        r = timed_get(api("/api/v1/stats"))
        results.append(strip(r, "stats"))
        time.sleep(1.1)  # ApiPolicy refills 1/s

    # 3. Search + images, cold vs cached as reported by the API itself.
    print(f"[3/5] search and images: {len(a.queries)} queries x {a.rounds} rounds (paced {a.pace}s)")
    thumb_urls = []
    for rnd in range(1, a.rounds + 1):
        for q in a.queries:
            for kind, path in (("search", "/api/v1/search"), ("images", "/api/v1/images")):
                r, d = get_json_paced(api(path, q=q), kind, results)
                if d is not None:
                    state = "cached" if d.get("cached") else "cold"
                    results.append(strip(r, f"{kind} {state}", query=q, round=rnd,
                                         server_ms=d.get("took_ms"), n_results=len(d.get("results", []))))
                    for hit in d.get("results", []):
                        t = (hit.get("thumb") or {}).get("url")
                        if t and t not in thumb_urls:
                            thumb_urls.append(t)
                else:
                    results.append(strip(r, f"{kind} error", query=q, round=rnd))
                time.sleep(a.pace)
            print(f"    round {rnd}: {q!r} done", flush=True)

    # 4. Thumbnails, twice each: the 2nd fetch should be a Cloudflare HIT.
    sample = [urllib.parse.urljoin(base + "/", u) for u in thumb_urls[: a.thumbs]]
    print(f"[4/5] thumbnails: {len(sample)} images x 2")
    for u in sample:
        for attempt in (1, 2):
            r = timed_get(u, accept="image/webp,image/*")
            results.append(strip(r, f"thumb {r.get('cf_cache', '-')}", attempt=attempt))
            time.sleep(0.2)

    # 5. Optional: AI summary through the Cloud Run relay.
    if a.summary:
        print("[5/5] AI summary x2 (the 2nd sighting is stored, so expect two cold calls)")
        for q in a.queries[:2]:
            r, d = get_json_paced(api("/api/v1/summary", q=q), "summary", results)
            label = "summary " + ("cached" if d and d.get("cached") else "cold" if d else "error")
            results.append(strip(r, label, query=q, server_ms=d.get("took_ms") if d else None,
                                 model=d.get("model") if d else None))
            time.sleep(21)
    else:
        print("[5/5] AI summary skipped (use --summary)")

    # ------------------------------------------------------------ report
    ok = [x for x in results if x.get("status") == 200]
    groups = {}
    for x in ok:
        groups.setdefault(x["label"], []).append(x)

    order = ["edge /", "health", "stats", "search cold", "search cached", "images cold", "images cached",
             "thumb MISS", "thumb HIT", "thumb DYNAMIC", "thumb EXPIRED", "thumb REVALIDATED",
             "summary cold", "summary cached"]
    rows = []
    for name in order + sorted(set(groups) - set(order)):
        if name in groups:
            srv = "server_ms" if name.split()[0] in ("search", "images", "summary") else None
            rows.append(summarize(name, groups[name], server=srv))

    conn = [x for x in ok if x.get("tls_ms") is not None]
    phases = {f: pct([x[f] for x in conn], 50) for f in ("dns_ms", "tcp_ms", "tls_ms", "ttfb_ms")}
    colos = sorted({x.get("colo") for x in ok if x.get("colo") not in (None, "-")})
    failures = [x for x in results if x.get("status") not in (200,) and "(429)" not in x["label"]]
    n429 = sum(1 for x in results if "(429)" in x["label"])
    thumb_bytes = [x["bytes"] for x in ok if x["label"].startswith("thumb") and x.get("bytes")]

    env = {"started_utc": started.isoformat(timespec="seconds"), "base": base,
           "client": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
           "cloudflare_colos": colos, "requests": len(results), "non_200": len(failures), "rate_limited_429": n429}

    lines = [f"# SearchBox benchmark, {started:%Y-%m-%d %H:%M} UTC", "",
             f"Target `{base}` · client {env['client']} · Cloudflare PoP {', '.join(colos) or '?'} · "
             f"{len(results)} requests, {len(failures)} errors, {n429} rate-limited (waited out)", "",
             "Every request used a new connection (DNS + TCP + TLS each time). Times in ms.", "",
             "| Request | n | p50 | p90 | p95 | max | server p50 | server p95 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        f = lambda v: "-" if v is None else f"{v:g}"
        lines.append(f"| {r['name']} | {r['n']} | {f(r['p50'])} | {f(r['p90'])} | {f(r['p95'])} | {f(r['max'])} | "
                     f"{f(r.get('server_p50'))} | {f(r.get('server_p95'))} |")
    lines += ["", "Median connection phases: " + ", ".join(f"{k[:-3].upper()} {v} ms" for k, v in phases.items()),
              ""]
    if thumb_bytes:
        lines.append(f"Thumbnail size: median {pct(thumb_bytes, 50) / 1024:.1f} KB, max {max(thumb_bytes) / 1024:.1f} KB")
    lines += ["", "`server` = the API's own `took_ms` (inside the app: cache lookup, embedding, SQL).",
              "The rest of the total is network: your machine -> Cloudflare -> edge VM (Hong Kong) -> app VM and back."]
    if failures:
        lines += ["", "Errors:"] + [f"- {x['label']}: status {x.get('status')} {x.get('error') or ''} {x['url']}"
                                    for x in failures[:15]]
    report = "\n".join(lines)

    with open(a.out + ".json", "w") as fh:
        json.dump({"environment": env, "summary": rows, "phases_p50_ms": phases, "requests": results}, fh, indent=2)
    with open(a.out + ".md", "w") as fh:
        fh.write(report + "\n")
    print("\n" + report)
    print(f"\nSaved {a.out}.md and {a.out}.json")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
