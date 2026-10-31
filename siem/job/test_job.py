"""Tests for the SIEM job with fake BigQuery/GCS clients and fake HTTP.
Run: python siem/job/test_job.py   (needs requirements.txt installed; no GCP access)"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BUCKET"] = "proj-edge-logs"
import main  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += not cond


# ---------------- fakes ----------------
class Blob:
    def __init__(self, name):
        self.name = name


class FakeGCS:
    def __init__(self, names):
        self.names = names

    def list_blobs(self, bucket, prefix):
        assert bucket == "proj-edge-logs" and prefix == "edge/"
        return [Blob(n) for n in self.names]


class Job:
    def __init__(self, rows=None, output_rows=0, fail=False):
        self._rows, self.output_rows, self.errors, self.fail = rows or [], output_rows, None, fail

    def result(self):
        if self.fail:
            raise RuntimeError("load failed")
        return self._rows


class FakeBQ:
    def __init__(self, loaded=(), candidates=(), fail_load=False):
        self.loaded, self.candidates, self.fail_load = list(loaded), list(candidates), fail_load
        self.uri_loads, self.json_loads, self.queries = [], [], []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        if "FROM `proj.siem.loaded_files`" in sql:
            return Job([{"name": n} for n in self.loaded])
        if sql.lstrip().startswith("SELECT client_ip"):
            return Job(self.candidates)
        return Job([])

    def load_table_from_uri(self, uris, table, job_config):
        self.uri_loads.append((uris, table, job_config))
        return Job(output_rows=42, fail=self.fail_load)

    def load_table_from_json(self, rows, table, job_config):
        self.json_loads.append((rows, table, job_config))
        return Job()


# ---------------- load ----------------
names = ["edge/20261031-010000-b.ndjson.gz", "edge/20261031-001500-a.ndjson.gz",
         "edge/20261031-003000-c.ndjson.gz", "edge/stray.txt"]
check("files_to_load: new .ndjson.gz only, oldest first",
      main.files_to_load(names, {"edge/20261031-003000-c.ndjson.gz"})
      == ["edge/20261031-001500-a.ndjson.gz", "edge/20261031-010000-b.ndjson.gz"])

bq = FakeBQ(loaded=["edge/20261031-003000-c.ndjson.gz"])
out = main.load_new_files(bq, FakeGCS(names), "proj")
uris, table, cfg = bq.uri_loads[0]
check("load: one job for the two new files", uris == ["gs://proj-edge-logs/edge/20261031-001500-a.ndjson.gz",
                                                      "gs://proj-edge-logs/edge/20261031-010000-b.ndjson.gz"])
check("load: appends NDJSON into edge_events", table == "proj.siem.edge_events"
      and cfg.source_format == "NEWLINE_DELIMITED_JSON" and cfg.write_disposition == "WRITE_APPEND"
      and cfg.ignore_unknown_values)
check("load: records both files as loaded", [r["name"] for r in bq.json_loads[0][0]] == [u.split("/", 3)[3] for u in uris]
      and bq.json_loads[0][1] == "proj.siem.loaded_files")
check("load: reports files and rows", out["files"] == 2 and out["rows"] == 42)

bq = FakeBQ(loaded=[n for n in names])
check("load: nothing new -> no load job", main.load_new_files(bq, FakeGCS(names), "proj") == {"files": 0, "rows": 0}
      and not bq.uri_loads)

bq = FakeBQ(fail_load=True)
try:
    main.load_new_files(bq, FakeGCS(names), "proj")
    raised = False
except RuntimeError:
    raised = True
check("load: a failed load is NOT marked as loaded (retried next hour)", raised and not bq.json_loads)

# ---------------- enrichment ----------------
check("is_public: public v4/v6 yes; private, CGNAT, loopback, junk no",
      main.is_public("8.8.8.8") and main.is_public("2606:4700::1111")
      and not any(main.is_public(x) for x in ["10.20.1.4", "100.64.0.1", "127.0.0.1", "192.168.1.5", "", "nope"]))

calls = []


def fake_get(url, headers=None):
    calls.append((url, headers))
    if "ipinfo.io" in url:
        return {"ip": "8.8.8.8", "asn": "AS15169", "as_name": "Google LLC", "as_domain": "google.com",
                "country_code": "US", "country": "United States"}
    if "abuseipdb" in url:
        return {"data": {"abuseConfidenceScore": 0, "totalReports": 3, "usageType": "Content Delivery Network",
                         "isp": "Google LLC", "isTor": False, "countryCode": "US"}}
    raise AssertionError(url)


e = main.Enricher("tok", "key", fake_get)
it = e.lookup("8.8.8.8", dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc))
check("lookup: both providers merged", (it.country, it.asn, it.as_name, it.abuse_score, it.total_reports, it.is_tor)
      == ("US", "AS15169", "Google LLC", 0, 3, False))
check("lookup: both API keys go in headers, never in URLs",
      any("abuseipdb" in u and h == {"Key": "key"} for u, h in calls)
      and any("ipinfo" in u and h == {"Authorization": "Bearer tok"} for u, h in calls)
      and not any("tok" in u or "key=" in u for u, _ in calls))


def limited_get(url, headers=None):
    if "abuseipdb" in url:
        raise main.RateLimited("abuseipdb")
    return fake_get(url, headers)


e = main.Enricher("tok", "key", limited_get)
e.lookup("8.8.8.8", dt.datetime.now(dt.timezone.utc))
n_before = len(calls)
it = e.lookup("1.1.1.1", dt.datetime.now(dt.timezone.utc))
check("429 stops that provider for the rest of the run", "abuseipdb" in e.stopped and it.asn == "AS15169"
      and all("abuseipdb" not in u for u, _ in calls[n_before:]))

e = main.Enricher("", "key", fake_get)
it = e.lookup("8.8.8.8", dt.datetime.now(dt.timezone.utc))
check("no IPinfo token: country falls back to AbuseIPDB's", it.country == "US" and it.asn is None)

# ---------------- enrich() end to end ----------------
now = dt.datetime(2026, 10, 31, 1, tzinfo=dt.timezone.utc)
bq = FakeBQ(candidates=[{"ip": "10.0.0.1", "first_seen": now}, {"ip": "8.8.8.8", "first_seen": now},
                        {"ip": "1.1.1.1", "first_seen": now}])
main.ENRICH_LIMIT = 1
out = main.enrich(bq, "proj", main.Enricher("tok", "key", fake_get))
merge_sql, merge_cfg = bq.queries[-1]
p = merge_cfg.query_parameters[0]
check("enrich: private IP skipped, limit respected", out["ips"] == 1)
check("enrich: one parameterised MERGE into ip_intel",
      merge_sql.lstrip().startswith("MERGE `proj.siem.ip_intel`") and "8.8.8.8" not in merge_sql
      and p.name == "rows" and len(p.values) == 1)
api_repr = p.to_api_repr()
check("enrich: MERGE parameter is ARRAY<STRUCT> with typed fields",
      api_repr["parameterType"]["type"] == "ARRAY"
      and api_repr["parameterType"]["arrayType"]["type"] == "STRUCT"
      and [f["name"] for f in api_repr["parameterType"]["arrayType"]["structTypes"]][:3] == ["ip", "first_seen", "country"])
check("enrich: no keys -> skipped, no queries", main.enrich(FakeBQ(), "proj", main.Enricher()) == {"skipped": "no API keys"})

print("all passed" if not failures else f"{failures} failed")
sys.exit(1 if failures else 0)
