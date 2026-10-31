"""SIEM job: runs hourly on Cloud Run (triggered by Cloud Scheduler).

1. LOAD    new gs://<bucket>/edge/*.ndjson.gz files (written by Vector on the
           edge VM) into siem.edge_events. BigQuery load jobs are free; each
           file is recorded in siem.loaded_files so it loads exactly once.
2. ENRICH  client IPs seen recently that have no intel (or intel older than
           14 days): IPinfo Lite (country, ASN) and AbuseIPDB (abuse score,
           usage type, Tor). At most ENRICH_LIMIT IPs per run, which keeps
           AbuseIPDB's free 1,000 checks a day safe (40 x 24 = 960).
           Results are written with one MERGE into siem.ip_intel.

Either API key may be missing; that provider is then skipped.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DATASET = os.environ.get("DATASET", "siem")
BUCKET = os.environ.get("BUCKET", "")
PREFIX = os.environ.get("PREFIX", "edge/")
ENRICH_LIMIT = int(os.environ.get("ENRICH_LIMIT", "40"))
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "6"))
REFRESH_DAYS = 14
MAX_FILES_PER_RUN = 5000  # a load job takes up to 10,000 URIs
HTTP_TIMEOUT = 10


# ---------------------------------------------------------------- load

def files_to_load(object_names: list[str], already_loaded: set[str]) -> list[str]:
    """Vector's .ndjson.gz files that haven't been loaded yet, oldest first."""
    fresh = [n for n in object_names if n.endswith(".ndjson.gz") and n not in already_loaded]
    return sorted(fresh)[:MAX_FILES_PER_RUN]


def load_new_files(bq, gcs, project: str) -> dict:
    from google.cloud import bigquery

    table = f"{project}.{DATASET}"
    names = [b.name for b in gcs.list_blobs(BUCKET, prefix=PREFIX)]
    loaded = {row["name"] for row in bq.query(f"SELECT name FROM `{table}.loaded_files`").result()}
    todo = files_to_load(names, loaded)
    if not todo:
        return {"files": 0, "rows": 0}

    job = bq.load_table_from_uri(
        [f"gs://{BUCKET}/{n}" for n in todo],
        f"{table}.edge_events",
        job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            ignore_unknown_values=True,
            max_bad_records=1000,  # one odd line must not block a whole hour
        ),
    )
    job.result()  # raises if the load failed; then nothing is marked as loaded

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    bq.load_table_from_json(
        [{"name": n, "loaded_at": now} for n in todo],
        f"{table}.loaded_files",
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema=[bigquery.SchemaField("name", "STRING", mode="REQUIRED"),
                    bigquery.SchemaField("loaded_at", "TIMESTAMP")],
        ),
    ).result()
    return {"files": len(todo), "rows": job.output_rows or 0, "bad_records": len(job.errors or [])}


# ---------------------------------------------------------------- enrich

class RateLimited(Exception):
    """The provider said 429: stop asking it for the rest of this run."""


def fetch_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(url.split("?")[0]) from None
        raise


def is_public(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def ipinfo_lookup(ip: str, token: str, get=fetch_json) -> dict:
    # Token in a header (not ?token=), so it never appears in a URL that might be logged.
    j = get(f"https://api.ipinfo.io/lite/{urllib.parse.quote(ip)}", {"Authorization": f"Bearer {token}"})
    return {
        "country": j.get("country_code"),
        "asn": j.get("asn"),
        "as_name": j.get("as_name"),
        "as_domain": j.get("as_domain"),
    }


def abuseipdb_lookup(ip: str, key: str, get=fetch_json) -> dict:
    q = urllib.parse.urlencode({"ipAddress": ip, "maxAgeInDays": 90})
    d = get(f"https://api.abuseipdb.com/api/v2/check?{q}", {"Key": key}).get("data") or {}
    return {
        "abuse_score": d.get("abuseConfidenceScore"),
        "total_reports": d.get("totalReports"),
        "usage_type": d.get("usageType"),
        "isp": d.get("isp"),
        "is_tor": d.get("isTor"),
        "country_fallback": d.get("countryCode"),
    }


@dataclass
class Intel:
    ip: str
    first_seen: dt.datetime
    country: str | None = None
    asn: str | None = None
    as_name: str | None = None
    as_domain: str | None = None
    abuse_score: int | None = None
    total_reports: int | None = None
    usage_type: str | None = None
    isp: str | None = None
    is_tor: bool | None = None


@dataclass
class Enricher:
    ipinfo_token: str = ""
    abuse_key: str = ""
    get: object = fetch_json
    stopped: set = field(default_factory=set)
    errors: int = 0

    def lookup(self, ip: str, first_seen: dt.datetime) -> Intel:
        it = Intel(ip=ip, first_seen=first_seen)
        if self.ipinfo_token and "ipinfo" not in self.stopped:
            try:
                for k, v in ipinfo_lookup(ip, self.ipinfo_token, self.get).items():
                    setattr(it, k, v)
            except RateLimited:
                self.stopped.add("ipinfo")
            except Exception:
                self.errors += 1
        if self.abuse_key and "abuseipdb" not in self.stopped:
            try:
                a = abuseipdb_lookup(ip, self.abuse_key, self.get)
                fallback = a.pop("country_fallback")
                for k, v in a.items():
                    setattr(it, k, v)
                it.country = it.country or fallback
            except RateLimited:
                self.stopped.add("abuseipdb")
            except Exception:
                self.errors += 1
        return it


CANDIDATES_SQL = """
SELECT client_ip AS ip, MIN(ts) AS first_seen, COUNT(*) AS events
FROM `{t}.edge_events`
WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @hours HOUR)
  AND client_ip IS NOT NULL AND client_ip != ''
  AND client_ip NOT IN (
    SELECT ip FROM `{t}.ip_intel`
    WHERE last_checked >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @refresh_days DAY))
GROUP BY client_ip
ORDER BY events DESC
LIMIT @limit
"""

MERGE_SQL = """
MERGE `{t}.ip_intel` AS t
USING UNNEST(@rows) AS s
ON t.ip = s.ip
WHEN MATCHED THEN UPDATE SET
  last_checked  = CURRENT_TIMESTAMP(),
  country       = COALESCE(s.country, t.country),
  asn           = COALESCE(s.asn, t.asn),
  as_name       = COALESCE(s.as_name, t.as_name),
  as_domain     = COALESCE(s.as_domain, t.as_domain),
  abuse_score   = COALESCE(s.abuse_score, t.abuse_score),
  total_reports = COALESCE(s.total_reports, t.total_reports),
  usage_type    = COALESCE(s.usage_type, t.usage_type),
  isp           = COALESCE(s.isp, t.isp),
  is_tor        = COALESCE(s.is_tor, t.is_tor)
WHEN NOT MATCHED THEN INSERT
  (ip, first_seen, last_checked, country, asn, as_name, as_domain,
   abuse_score, total_reports, usage_type, isp, is_tor)
  VALUES (s.ip, s.first_seen, CURRENT_TIMESTAMP(), s.country, s.asn, s.as_name, s.as_domain,
   s.abuse_score, s.total_reports, s.usage_type, s.isp, s.is_tor)
"""

_FIELDS = [
    ("ip", "STRING"), ("first_seen", "TIMESTAMP"), ("country", "STRING"), ("asn", "STRING"),
    ("as_name", "STRING"), ("as_domain", "STRING"), ("abuse_score", "INT64"),
    ("total_reports", "INT64"), ("usage_type", "STRING"), ("isp", "STRING"), ("is_tor", "BOOL"),
]


def merge_params(intel: list[Intel]):
    """@rows as ARRAY<STRUCT<...>>: one MERGE for the whole batch, values passed as
    parameters (never pasted into the SQL text)."""
    from google.cloud import bigquery

    structs = [
        bigquery.StructQueryParameter(
            None, *[bigquery.ScalarQueryParameter(name, typ, getattr(it, name)) for name, typ in _FIELDS]
        )
        for it in intel
    ]
    return [bigquery.ArrayQueryParameter("rows", "STRUCT", structs)]


def enrich(bq, project: str, enricher: Enricher) -> dict:
    from google.cloud import bigquery

    if not (enricher.ipinfo_token or enricher.abuse_key):
        return {"skipped": "no API keys"}
    t = f"{project}.{DATASET}"
    rows = bq.query(
        CANDIDATES_SQL.format(t=t),
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("hours", "INT64", LOOKBACK_HOURS),
            bigquery.ScalarQueryParameter("refresh_days", "INT64", REFRESH_DAYS),
            bigquery.ScalarQueryParameter("limit", "INT64", ENRICH_LIMIT * 3),
        ]),
    ).result()
    intel = []
    for row in rows:
        if len(intel) >= ENRICH_LIMIT:
            break
        if is_public(row["ip"]):
            intel.append(enricher.lookup(row["ip"], row["first_seen"]))
    if intel:
        bq.query(MERGE_SQL.format(t=t),
                 job_config=bigquery.QueryJobConfig(query_parameters=merge_params(intel))).result()
    return {"ips": len(intel), "errors": enricher.errors, "rate_limited": sorted(enricher.stopped)}


# ---------------------------------------------------------------- main

def main() -> int:
    if not BUCKET:
        print("BUCKET is not set", file=sys.stderr)
        return 2
    from google.cloud import bigquery, storage

    bq = bigquery.Client()
    project = bq.project
    gcs = storage.Client(project=project)
    status = 0
    try:
        print(json.dumps({"step": "load", **load_new_files(bq, gcs, project)}), flush=True)
    except Exception as e:  # report and carry on: enrichment can still use older rows
        print(json.dumps({"step": "load", "error": f"{type(e).__name__}: {e}"[:500]}), flush=True)
        status = 1
    try:
        enricher = Enricher(os.environ.get("IPINFO_TOKEN", "").strip(), os.environ.get("ABUSEIPDB_KEY", "").strip())
        print(json.dumps({"step": "enrich", **enrich(bq, project, enricher)}), flush=True)
    except Exception as e:
        print(json.dumps({"step": "enrich", "error": f"{type(e).__name__}: {e}"[:500]}), flush=True)
        status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
