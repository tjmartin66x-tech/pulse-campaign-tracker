#!/usr/bin/env python3
"""Extract unique company domains from FIERCE_PC campaign leads."""
import os, json, sys
from urllib.request import urlopen, Request
from urllib.parse import urlencode

BASE = "https://api.instantly.ai/api/v2"
KEY  = os.environ.get("INSTANTLY_API_KEY", "").strip()
if not KEY:
    sys.exit("ERROR: INSTANTLY_API_KEY not set")

HEADERS = {"Authorization": f"Bearer {KEY}",
           "Content-Type": "application/json",
           "User-Agent": "Mozilla/5.0"}

CAMPAIGN_IDS = [
    "ef22cd8a-c292-4323-9e82-c17e09ecebe6",  # FIERCE_PC_Finance_C1
    "d2b775d2-f5cb-4bb8-97ce-665492dab747",  # FIERCE_PC_Claims_C1
    "c5a68950-bb76-4d37-a054-cc3e373c7546",  # FIERCE_PC_Risk_C1
]

domains = set()
for cid in CAMPAIGN_IDS:
    cursor = None
    while True:
        body = json.dumps({"campaign": cid, "limit": 100,
                           **({"starting_after": cursor} if cursor else {})}).encode()
        req = Request(f"{BASE}/leads/list", data=body, method="POST", headers=HEADERS)
        with urlopen(req, timeout=30) as r:
            page = json.loads(r.read())
        for lead in page.get("items", []):
            email = lead.get("email", "")
            if "@" in email:
                domains.add(email.split("@")[1].lower().strip())
        cursor = page.get("next_starting_after")
        if not cursor or not page.get("items"):
            break

print(f"UNIQUE_DOMAINS ({len(domains)}):")
for d in sorted(domains):
    print(d)
