#!/usr/bin/env python3
"""
Rebake the FP_S1_PAY dashboard (index.html) from live Instantly data.

Two responsibilities:
  1. SEND PERFORMANCE  - pulled from /campaigns/analytics
  2. LEAD INVENTORY    - pulled from /leads/list, reconciled into the roster so
                         net-new leads auto-appear and the headline count never
                         lags behind Instantly.

Design / safety:
  - Existing enriched lead rows are PRESERVED verbatim (matched by email); only
    genuinely new leads are normalized and appended, and removed leads are dropped.
  - Every summary breakdown is recomputed from the reconciled roster.
  - HARD GUARDRAILS: the file is only written if all invariants hold
    (roster length == total, every breakdown sums to total, no catastrophic
    drop). On any failure the script prints VALIDATION_FAILED and exits non-zero
    WITHOUT touching index.html - an unattended run can never corrupt the board.
  - Prints "NO_CHANGE" (and writes nothing) when nothing material changed.
  - Run `python3 scripts/rebake.py --selftest` to validate the rebuild logic
    against the current committed DATA without any network calls.

Reads INSTANTLY_API_KEY from the environment.
"""
import os, re, json, sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from urllib.parse import urlencode
from collections import Counter, OrderedDict

BASE = "https://api.instantly.ai/api/v2"
HTML_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "index.html"))
VALID_TEMPLATES = {"A_SUBSCRIPTION", "B_HEALTHCARE", "D_BANKING", "E_INSURANCE"}
DIM_FIELDS = [  # (signal_activity key, lead-record field)
    ("signal_band", "signal_band"), ("target_dm", "target_dm"),
    ("template", "template"), ("tier", "tier"), ("p_class", "p_class"),
    ("campaign_label", "campaign_name"), ("csc", "csc"),
]

# ----------------------------------------------------------------------------- HTTP
def _api(path, params=None, method="GET", body=None):
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()   # strip paste artifacts
    if not key:
        sys.exit("ERROR: INSTANTLY_API_KEY not set")
    url = BASE + path + (("?" + urlencode(params)) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method,
                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                           # Instantly is behind Cloudflare; the default Python-urllib UA trips
                           # CF bot protection (error 1010). A browser UA passes.
                           "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                           "Accept": "application/json"})
    try:
        with urlopen(req, timeout=45) as r:
            return json.loads(r.read())
    except HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        print(f"API_ERROR {e.code} {method} {path}: {detail}", file=sys.stderr)
        raise

def fetch_analytics():
    a = _api("/campaigns/analytics")           # GET
    return a if isinstance(a, list) else []

def fetch_all_leads():
    """Page through every lead in the workspace. /leads/list is a POST in v2."""
    out, cursor = [], None
    while True:
        body = {"limit": 100}
        if cursor:
            body["starting_after"] = cursor
        page = _api("/leads/list", method="POST", body=body)
        items = page.get("items", []) if isinstance(page, dict) else (page or [])
        out.extend(items)
        cursor = (page.get("next_starting_after")
                  or (page.get("pagination") or {}).get("next_starting_after")) if isinstance(page, dict) else None
        if not cursor or not items:
            return out

# ----------------------------------------------------------------------------- normalize
def short_name(camp_name):
    return camp_name.replace("FP_S1_", "").replace("_", " ")

def norm_tier(raw):
    # underscores -> spaces so "PASS_TIER_2" matches the "PASS TIER 3" house style
    t = (raw or "").strip().upper().replace("_", " ")
    return {"T1": "TIER 1", "T2": "TIER 2", "T3": "TIER 3"}.get(t, t) or "TIER 1"

def norm_band(raw):
    b = (raw or "").strip().upper()
    return b if b in {"LOW", "MEDIUM", "HIGH"} else "MEDIUM"

def norm_csc(raw):
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return "0"

def derive_template(payload):
    """payload.template if valid, else keyword-derive; '' when genuinely unknown
    (an honest blank bucket beats a fabricated classification)."""
    tpl = (payload.get("template") or "").strip().upper()
    if tpl in VALID_TEMPLATES:
        return tpl
    text = " ".join(str(payload.get(k, "")) for k in
                    ("companyName", "title", "workflow_phrase", "subject", "surface", "p_class")).lower()
    if any(k in text for k in ("mortgage", "home loan", "loan agency", "loan ", "bank", "credit union",
                               "deposit", "servicing", "lending", "disburse", "treasury")):
        return "D_BANKING"
    if any(k in text for k in ("insur", "parametric", "underwrit", "carrier", "actuar", "claim")):
        return "E_INSURANCE"
    if any(k in text for k in ("health", "payer", "hospital", "patient", "revenue cycle", "rcm")):
        return "B_HEALTHCARE"
    if any(k in text for k in ("royalt", "subscription", "saas", "recurring", "peo", "payroll")):
        return "A_SUBSCRIPTION"
    return ""  # unknown - leave blank rather than guess

def build_record(ll, camp_name):
    """Construct a dashboard lead record from a live Instantly lead."""
    p = ll.get("payload") or {}
    body = p.get("body", "") or ""
    return {
        "email": ll.get("email", ""),
        "first_name": p.get("firstName") or ll.get("first_name", ""),
        "last_name": p.get("lastName") or ll.get("last_name", ""),
        "company": ll.get("company_name") or p.get("companyName", ""),
        "title": p.get("title", "") or "",
        "website": p.get("website") or ll.get("website", ""),
        "tier": norm_tier(p.get("tier")),
        "signal_band": norm_band(p.get("signal_band")),
        "csc": norm_csc(p.get("csc")),
        "composite": p.get("composite", "") or "",
        "p_class": p.get("p_class", "") or "",
        "target_dm": p.get("target_dm", "") or "",
        "instantly_tag": p.get("instantly_tag", "") or "",
        "workflow_phrase": p.get("workflow_phrase", "") or "",
        "subject": p.get("subject", "") or "",
        "body_preview": body.replace("\n", "\\n"),
        "body_word_count": len(body.split()),
        "personalization": p.get("personalization") or ll.get("personalization", "") or "",
        "template": derive_template(p),
        "match_path": p.get("match_path", "") or "(unknown)",
        "priority": p.get("priority", "") or "P3",
        "campaign_name": camp_name,
        "surface_evidence": p.get("surface_evidence", "") or "",
    }

# ----------------------------------------------------------------------------- rebuild
def recompute_summary(summary, roster):
    summary["total_leads"] = len(roster)
    summary["tenant_total"] = len(roster)
    summary["by_campaign"] = dict(Counter(short_name(l["campaign_name"]) for l in roster))
    summary["by_tier"] = dict(Counter(l["tier"] for l in roster))
    summary["by_band"] = dict(Counter(l["signal_band"] for l in roster))
    summary["by_template"] = dict(Counter(l["template"] for l in roster))
    summary["by_target_dm"] = dict(Counter(l["target_dm"] for l in roster))
    summary["by_p_class"] = dict(Counter(l["p_class"] for l in roster))
    summary["by_match_path"] = dict(Counter(l["match_path"] for l in roster))
    summary["csc_dist"] = dict(Counter(str(l["csc"]) for l in roster))

def rebuild_signal_activity(sa, live_rows, ts):
    """live_rows: list of (record, open_count, reply_count, click_count)."""
    for sa_key, field in DIM_FIELDS:
        agg = OrderedDict()
        for rec, o, rp, c in live_rows:
            val = rec[field]
            if sa_key == "campaign_label":
                val = short_name(val)
            elif sa_key == "p_class":
                val = val or "(blank)"
            elif sa_key == "csc":
                val = str(val)
            row = agg.setdefault(val, {"key": val, "leads": 0, "sent": 0, "opens": 0, "replies": 0, "clicks": 0})
            row["leads"] += 1; row["sent"] += 1
            row["opens"] += o; row["replies"] += rp; row["clicks"] += c
        sa[sa_key] = sorted(agg.values(), key=lambda r: -r["leads"])
    sa.setdefault("_summary", {})
    sa["_summary"].update({"pulled_at": ts, "joined_leads": len(live_rows),
                           "inventory_total": len(live_rows)})

def update_performance(perf, analytics, campaigns):
    by_name = {a["campaign_name"]: a for a in analytics}
    for name, row in perf["per_campaign"].items():
        a = by_name.get(name)
        if not a:
            continue
        row.update(leads_count=a.get("leads_count", row.get("leads_count", 0)),
                   sent=a.get("emails_sent_count", 0), opens=a.get("open_count", 0),
                   opens_unique=a.get("open_count_unique", 0), replies=a.get("reply_count", 0),
                   replies_unique=a.get("reply_count_unique", 0), clicks=a.get("link_click_count", 0),
                   bounces=a.get("bounced_count", 0), unsubs=a.get("unsubscribed_count", 0),
                   completed=a.get("completed_count", 0), status=a.get("campaign_status", row.get("status", 0)))
    tot = {k: 0 for k in ("sent", "opens", "opens_unique", "clicks", "replies",
                          "replies_unique", "bounces", "unsubs", "completed", "leads")}
    for row in perf["per_campaign"].values():
        for k in tot:
            tot[k] += row.get("leads_count" if k == "leads" else k, 0)
    s = tot["sent"] or 1
    perf["totals"] = tot
    perf["rates"] = {"open_rate": round(tot["opens"] / s * 100, 2),
                     "open_rate_unique": round(tot["opens_unique"] / s * 100, 2),
                     "reply_rate": round(tot["replies"] / s * 100, 2),
                     "reply_rate_unique": round(tot["replies_unique"] / s * 100, 2),
                     "bounce_rate": round(tot["bounces"] / s * 100, 2)}

def validate(d, prev_total):
    """Return list of failure strings; empty == OK."""
    s, roster = d["summary"], d["leads"]
    n = s["total_leads"]
    fails = []
    if len(roster) != n:
        fails.append(f"roster len {len(roster)} != total_leads {n}")
    if sum(s["by_campaign"].values()) != n:
        fails.append(f"by_campaign sums {sum(s['by_campaign'].values())} != {n}")
    for key in ("by_tier", "by_band", "by_template", "by_target_dm", "by_p_class", "by_match_path", "csc_dist"):
        if sum(s[key].values()) != n:
            fails.append(f"{key} sums {sum(s[key].values())} != {n}")
    if prev_total and n < prev_total * 0.5:
        fails.append(f"catastrophic drop: {n} < 50% of prior {prev_total}")
    return fails

# ----------------------------------------------------------------------------- io
def load_html():
    lines = open(HTML_PATH).readlines()
    m = re.match(r"const DATA = (.*);\s*$", lines[312].strip())
    if not m:
        sys.exit("ERROR: DATA blob not found on line 313")
    return lines, json.loads(m.group(1))

def write_html(lines, d, ts):
    lines[312] = "const DATA = " + json.dumps(d, separators=(",", ":"), ensure_ascii=True) + ";\n"
    n = d["summary"]["total_leads"]
    lines[130] = re.sub(r'(fresh-tag">)\d+ leads loaded(<)', rf"\g<1>{n} leads loaded\2", lines[130])
    lines[130] = re.sub(r'(<span class="live-tag">)[^<]*(</span>)', rf"\g<1>{ts}\2", lines[130])
    lines[307] = re.sub(r"Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", f"Generated {ts}", lines[307])
    open(HTML_PATH, "w").writelines(lines)

# ----------------------------------------------------------------------------- selftest
def selftest():
    _, d = load_html()
    roster = d["leads"]
    s = d["summary"]
    chk = {"by_tier": "tier", "by_band": "signal_band", "by_template": "template",
           "by_target_dm": "target_dm", "by_p_class": "p_class", "by_match_path": "match_path"}
    ok = True
    for key, field in chk.items():
        if dict(Counter(l.get(field) for l in roster)) != s[key]:
            print(f"  FAIL {key} derivation mismatch"); ok = False
    # normalization spot checks
    assert norm_tier("T2") == "TIER 2" and norm_tier("TIER 3") == "TIER 3"
    assert derive_template({"companyName": "Frontline Insurance", "workflow_phrase": "claims payout"}) == "E_INSURANCE"
    assert derive_template({"companyName": "Flat Branch Home Loans", "workflow_phrase": "servicing"}) == "D_BANKING"
    assert build_record({"email": "x@y.com", "payload": {"tier": "T2", "body": "a\nb"}}, "FP_S1_PAY_T2L")["body_preview"] == "a\\nb"
    print("SELFTEST", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

# ----------------------------------------------------------------------------- main
def main():
    if "--selftest" in sys.argv:
        selftest()

    lines, d = load_html()
    prev_total = d["summary"]["total_leads"]
    prev_perf = json.loads(json.dumps(d["performance"]))
    campaigns = d["campaigns"]
    id_to_name = {c["id"]: c["name"] for c in campaigns}
    fp_ids = set(id_to_name)

    # ---- performance
    update_performance(d["performance"], fetch_analytics(), campaigns)

    # ---- inventory reconcile
    baked = {l["email"]: l for l in d["leads"]}
    roster, live_rows = [], []
    for ll in fetch_all_leads():
        cid = ll.get("campaign")
        if cid not in fp_ids:           # only leads in our FP_S1 campaigns
            continue
        email = ll.get("email")
        rec = baked.get(email) or build_record(ll, id_to_name[cid])
        rec["campaign_name"] = id_to_name[cid]   # keep campaign current on moves
        roster.append(rec)
        live_rows.append((rec, ll.get("email_open_count", 0) or 0,
                          ll.get("email_reply_count", 0) or 0, ll.get("email_click_count", 0) or 0))
    d["leads"] = roster
    recompute_summary(d["summary"], roster)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rebuild_signal_activity(d["signal_activity"], live_rows, ts)

    # ---- guardrails
    fails = validate(d, prev_total)
    if fails:
        print("VALIDATION_FAILED: " + "; ".join(fails))
        sys.exit(2)

    # ---- change detection
    if (len(roster) == prev_total and d["performance"]["totals"] == prev_perf["totals"]
            and d["performance"]["rates"] == prev_perf["rates"]):
        print("NO_CHANGE")
        sys.exit(0)

    d["performance"]["pulled_at"] = ts
    d["meta"]["last_refresh"] = ts
    d["meta"]["last_load_batch"] = f"auto-rebake · {len(roster)} active + {d.get('_quarantine_count', 0)} quarantined"
    write_html(lines, d, ts)

    tot = d["performance"]["totals"]
    active = sum(1 for c in campaigns if c.get("status") == 1)
    print(f"Auto-rebake {ts}: {len(roster)} active leads, {tot['sent']} sent, "
          f"{tot['opens']} opens, {tot['replies']} replies, {active} active campaigns")

if __name__ == "__main__":
    main()
