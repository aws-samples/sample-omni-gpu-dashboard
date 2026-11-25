"""Run every PromQL panel in an Omni dashboard body against CloudWatch and report which return data.

Usage: python build_dashboard.py --cluster <cluster> --out body.json && AWS_REGION=<region> python check_queries.py body.json
"""

import json
import os
import re
import sys
import urllib.parse

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session

REGION = os.environ.get("AWS_REGION", "us-east-1")
if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", REGION):
    sys.exit(f"AWS_REGION {REGION!r} is not a Region name such as us-east-1")
# Fixed HTTPS endpoint: the only variable part is the validated Region.
URL = f"https://monitoring.{REGION}.amazonaws.com/api/v1/query"
creds = boto3.Session().get_credentials().get_frozen_credentials()
http = URLLib3Session(timeout=60)


def query(q: str) -> tuple[str, int]:
    """POST one instant query to CloudWatch's Prometheus-compatible API, signed with SigV4."""
    req = AWSRequest("POST", URL, data=urllib.parse.urlencode({"query": q}),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    SigV4Auth(creds, "monitoring", REGION).add_auth(req)
    resp = http.send(req.prepare())
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}: {resp.text[:200]}", 0
    d = json.loads(resp.content)
    return d.get("status", "?"), len(d.get("data", {}).get("result", []))


with open(sys.argv[1]) as f:
    panels = json.load(f)["panels"]
empty = 0
for p in panels:
    cfg = p.get("config", {})
    if cfg.get("queryLanguage") != "promql":
        continue
    status, n = query(cfg["queryString"])
    empty += n == 0
    print(f"{'ok  ' if n else 'EMPTY'} {n:>3}  {p['title'][:70]:<70} {'' if status == 'success' else status}")
print(f"\n{empty} queries returned no series")
