#!/usr/bin/env python3
"""
Rebake the FP_S1_PAY dashboard (index.html) with fresh data from Instantly.
Reads INSTANTLY_API_KEY from the environment.
Exits 0 with message "NO_CHANGE" if metrics are unchanged (skip empty commits).
"""
import os, re, json, sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError

API_KEY = os.environ.get("INSTANTLY_API_KEY", "")
if not API_KEY:
    sys.exit("ERROR: INSTANTLY_API_KEY not set")

BASE = "https://api.instantly.ai/api/v2"
HEADERS = {"Authorization": API_KEY, "Content-Type": "application/json"}

def get(path, params=None):
    url = BASE + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# ── 1. Fetch analytics (all campaigns) ──────────────────────────────────────
try:
    analytics = get("/campaigns/analytics")
except HTTPError as e:
    sys.exit(f"ERROR fetching analytics: {e.code} {e.reason}\n{e.read().decode()}")

if not isinstance(analytics, list):
    sys.exit(f"ERROR: unexpected analytics shape: {type(analytics)}")

# ── 2. Load current index.html ───────────────────────────────────────────────
html_path = os.path.join(os.path.dirname(__file__), "..", "index.html")
html_path = os.path.normpath(html_path)
lines = open(html_path).readlines()
raw = lines[312].strip()
m = re.match(r"const DATA = (.*);\s*$", raw)
if not m:
    sys.exit("ERROR: could not find DATA blob on line 313")
d = json.loads(m.group(1))

# ── 3. Build per-campaign metrics from analytics response ────────────────────
# Sender map — preserve existing senders from baked campaigns list
sender_map = {c["name"]: c.get("sender", "") for c in d["campaigns"]}

analytics_by_name = {a["campaign_name"]: a for a in analytics}
pc = d["performance"]["per_campaign"]

for name, row in pc.items():
    a = analytics_by_name.get(name)
    if not a:
        continue
    row["leads_count"] = a.get("leads_count", row.get("leads_count", 0))
    row["sent"]         = a.get("emails_sent_count", 0)
    row["opens"]        = a.get("open_count", 0)
    row["opens_unique"] = a.get("open_count_unique", 0)
    row["replies"]      = a.get("reply_count", 0)
    row["replies_unique"]= a.get("reply_count_unique", 0)
    row["clicks"]       = a.get("link_click_count", 0)
    row["bounces"]      = a.get("bounced_count", 0)
    row["unsubs"]       = a.get("unsubscribed_count", 0)
    row["completed"]    = a.get("completed_count", 0)
    row["status"]       = a.get("campaign_status", row.get("status", 0))

# ── 4. Recompute totals ───────────────────────────────────────────────────────
keys = ["sent","opens","opens_unique","clicks","replies","replies_unique",
        "bounces","unsubs","completed","leads"]
tot = {k: 0 for k in keys}
for row in pc.values():
    tot["sent"]          += row.get("sent", 0)
    tot["opens"]         += row.get("opens", 0)
    tot["opens_unique"]  += row.get("opens_unique", 0)
    tot["clicks"]        += row.get("clicks", 0)
    tot["replies"]       += row.get("replies", 0)
    tot["replies_unique"]+= row.get("replies_unique", 0)
    tot["bounces"]       += row.get("bounces", 0)
    tot["unsubs"]        += row.get("unsubs", 0)
    tot["completed"]     += row.get("completed", 0)
    tot["leads"]         += row.get("leads_count", 0)

s = tot["sent"] or 1
rates = {
    "open_rate":        round(tot["opens"]          / s * 100, 2),
    "open_rate_unique": round(tot["opens_unique"]   / s * 100, 2),
    "reply_rate":       round(tot["replies"]        / s * 100, 2),
    "reply_rate_unique":round(tot["replies_unique"] / s * 100, 2),
    "bounce_rate":      round(tot["bounces"]        / s * 100, 2),
}

# ── 5. Check for changes ──────────────────────────────────────────────────────
old_tot   = d["performance"]["totals"]
old_rates = d["performance"]["rates"]
changed = (tot != old_tot or rates != old_rates)

if not changed:
    print("NO_CHANGE")
    sys.exit(0)

# ── 6. Apply updates ──────────────────────────────────────────────────────────
ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

d["performance"]["totals"]   = tot
d["performance"]["rates"]    = rates
d["performance"]["pulled_at"]= ts
d["meta"]["last_refresh"]    = ts
d["meta"]["last_load_batch"] = (
    f"auto-rebake · {d['summary']['total_leads']} active + "
    f"{d.get('_quarantine_count', 0)} quarantined"
)
if "_summary" in d.get("signal_activity", {}):
    d["signal_activity"]["_summary"]["pulled_at"] = ts

# ── 7. Write updated HTML ─────────────────────────────────────────────────────
lines[312] = "const DATA = " + json.dumps(d, separators=(",",":"), ensure_ascii=True) + ";\n"

# Header timestamp
header_pat = re.compile(r'(<span class="live-tag">)[^<]*(</span>)')
lines[130] = header_pat.sub(rf'\g<1>{ts}\2', lines[130])

# Footer timestamp
footer_pat = re.compile(r'Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC')
lines[307] = footer_pat.sub(f"Generated {ts}", lines[307])

open(html_path, "w").writelines(lines)

# ── 8. Print commit-message summary for the workflow to capture ───────────────
active = sum(1 for c in d["campaigns"] if c.get("status") == 1)
tl = d["summary"]["total_leads"]
print(f"Auto-rebake {ts}: {tl} active leads, {tot['sent']} sent, "
      f"{tot['opens']} opens, {tot['replies']} replies, {active} active campaigns")
