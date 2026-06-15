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
VERSION_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "version.txt"))
VALID_TEMPLATES = {"A_SUBSCRIPTION", "B_HEALTHCARE", "D_BANKING", "E_INSURANCE"}
# Canonical Holds & Flags — the auto-rebake enforces exactly these every run, so
# retired flags (INTERLEAVED, QUARANTINED, E2-E5, NEW LOAD) can't drift back in.
CANONICAL_HOLDS = [
    {"tag": "GATE", "text": "No campaign activates without explicit Troy 'Y' (Sprint v2.0)"},
    {"tag": "COLD", "text": "Stream-G runtime claims FALSE in cold copy — NDA-anonymous follow-up only"},
    {"tag": "ROT", "text": "Klarna excluded from PAYOUT; CFPB Circular 2023-03, EO 14110, SB 1047, Colorado SB 24-205, Click-to-Cancel all rotted"},
    {"tag": "WARMUP", "text": "Domain A warmup must hit ≥95% inbox placement before activation"},
    {"tag": "HOLD", "text": "PlainsCapital Bank — source CSV had blank target_dm + tier. Apollo surfaced Walter Cl***e (CFO). Needs Troy tier assignment for next-pass load."},
]
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

def fetch_campaigns():
    """All campaigns (incl. empty drafts), so newly-created campaigns are seen
    even before they have leads. Defensive: returns [] if the endpoint errors."""
    out, cursor = [], None
    try:
        while True:
            params = {"limit": 100}
            if cursor:
                params["starting_after"] = cursor
            page = _api("/campaigns", params)
            items = page.get("items", []) if isinstance(page, dict) else (page or [])
            out.extend(items)
            cursor = (page.get("next_starting_after")
                      or (page.get("pagination") or {}).get("next_starting_after")) if isinstance(page, dict) else None
            if not cursor or not items:
                break
    except Exception as e:
        print(f"WARN fetch_campaigns failed ({e}); falling back to analytics discovery", file=sys.stderr)
    return out

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

def emails_sent_step(ll):
    """Number of sequence emails sent to this lead (E1..EN), from
    status_summary.lastStep.stepID like '0_2_0' (middle = 0-based step index)."""
    sid = ((ll.get("status_summary") or {}).get("lastStep") or {}).get("stepID")
    if not sid:
        return 0
    try:
        return int(sid.split("_")[1]) + 1
    except (IndexError, ValueError):
        return 0

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
        "match_path": p.get("match_path", "") or "(not recorded)",
        "priority": p.get("priority", "") or "P3",
        "campaign_name": camp_name,
        "surface_evidence": p.get("surface_evidence", "") or "",
        "opens": 0, "replies": 0, "clicks": 0, "sent_step": 0, "lead_status": 0,
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

STATUS_MAP = {0: "Draft", 1: "Active", 2: "Paused", 3: "Completed", 4: "Stopped"}

# One concise thesis per lane (keyed by the prefix after FP_S1_), stamped onto every
# campaign card each rebake so each box states what the lane is testing. Grounded in the
# lane's actual cold-copy hook + target persona/segment. Edit text here; it self-applies.
LANE_THESES = {
    "PAY":   "Execution-integrity gate at the payout commit — CFO/Risk where AI now moves the money (subscription · banking · healthcare).",
    "REF":   "The same gate at the refund/credit commit — Controllers at subscription businesses issuing AI-driven refunds.",
    "EAX":   "The funding commit as AI scales — bank & insurer CFOs on application-to-funding.",
    "MA":    "M&A-trigger lane — payout-commit integrity to bank CFO/Risk during M&A events; tests M&A copy vs generic.",
    "MRM":   "Model-risk angle — the gap OCC 2026-13 leaves on agentic AI, pitched to bank/insurer CFOs.",
    "NAIC":  "Insurance-reg hook — the NAIC AI exam-tool pilot, to insurer CFOs on claim-to-payout.",
    "FINRA": "Broker-dealer hook — FINRA's four questions on AI agents, to Chief Compliance Officers.",
}

def _lane(name):
    return name.replace("FP_S1_", "").split("_")[0]

def discover_campaigns(d, campaigns_list, analytics):
    """Sync the tracked campaign cards with Instantly: add any new FP_S1 campaign
    (including empty drafts, from the campaigns endpoint) and refresh status on
    existing cards. Falls back to analytics rows if the campaigns list is empty."""
    by_id = {c["id"]: c for c in d["campaigns"]}
    by_name = {c["name"]: c for c in d["campaigns"]}
    # unified source rows: (name, id, status, sender) — full list first, analytics as fallback
    rows, seen = [], set()
    for c in (campaigns_list or []):
        rows.append((c.get("name", ""), c.get("id"), c.get("status", 0), (c.get("email_list") or [""])[0]))
        seen.add(c.get("id"))
    for a in analytics:
        if a.get("campaign_id") not in seen:
            rows.append((a.get("campaign_name", ""), a.get("campaign_id"), a.get("campaign_status", 1), ""))
    for name, cid, st, sender in rows:
        if not name.startswith("FP_S1_"):
            continue
        card = by_id.get(cid) or by_name.get(name)
        if card:                                   # refresh status of an existing card
            card["status"] = st
            card["status_label"] = STATUS_MAP.get(st, card.get("status_label", "Unknown"))
            if sender and not card.get("sender"):
                card["sender"] = sender
        else:                                      # add a newly-created campaign
            band = "C8" if "C8" in name else "T1" if "T1" in name else "T2" if "T2" in name else ""
            card = {"name": name, "id": cid, "status": st,
                    "status_label": STATUS_MAP.get(st, "Unknown"), "band": band,
                    "sender": sender or "", "expected": 0, "primary": st == 1}
            d["campaigns"].append(card)
            by_id[cid] = card
            by_name[name] = card
        thesis = LANE_THESES.get(_lane(name))      # stamp the lane thesis onto the card
        if thesis:
            card["thesis"] = thesis

def update_performance(perf, analytics, campaigns):
    by_name = {a["campaign_name"]: a for a in analytics}
    pc = perf["per_campaign"]
    # ensure a row exists for every campaign card, and keep status/sender synced to the
    # live campaign list (authoritative) — so a draft->active flip shows in the Per Campaign
    # table immediately, even before the campaign has any sends to appear in analytics.
    for c in campaigns:
        row = pc.setdefault(c["name"], {"status": c.get("status", 0), "sender": c.get("sender", ""),
                                  "leads_count": 0, "sent": 0, "completed": 0, "opens": 0,
                                  "opens_unique": 0, "replies": 0, "replies_unique": 0,
                                  "clicks": 0, "bounces": 0, "unsubs": 0})
        row["status"] = c.get("status", row.get("status", 0))
        if c.get("sender") and not row.get("sender"):
            row["sender"] = c.get("sender")
    for name, row in pc.items():
        a = by_name.get(name)
        if not a:
            continue
        # send metrics come from analytics; status stays from the live card above,
        # leads_count is set authoritatively from the roster (see sync_campaign_leadcounts).
        row.update(sent=a.get("emails_sent_count", 0), opens=a.get("open_count", 0),
                   opens_unique=a.get("open_count_unique", 0), replies=a.get("reply_count", 0),
                   replies_unique=a.get("reply_count_unique", 0), clicks=a.get("link_click_count", 0),
                   bounces=a.get("bounced_count", 0), unsubs=a.get("unsubscribed_count", 0),
                   completed=a.get("completed_count", 0))
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

def sync_campaign_leadcounts(perf, roster):
    """Per-campaign lead counts come from the reconciled roster (authoritative and never
    lags), not analytics' delayed count — so a freshly-activated campaign shows its real
    inventory immediately instead of a stale 0. Keeps the totals leads figure in step."""
    counts = Counter(l["campaign_name"] for l in roster)
    for name, row in perf["per_campaign"].items():
        row["leads_count"] = counts.get(name, 0)
    perf.setdefault("totals", {})["leads"] = sum(counts.values())

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
def _find(lines, pred, what):
    for i, l in enumerate(lines):
        if pred(l):
            return i
    sys.exit(f"ERROR: could not locate {what} in index.html")

def load_html():
    lines = open(HTML_PATH).readlines()
    di = _find(lines, lambda l: l.startswith("const DATA = "), "DATA blob")
    m = re.match(r"const DATA = (.*);\s*$", lines[di].strip())
    if not m:
        sys.exit("ERROR: DATA blob malformed")
    return lines, json.loads(m.group(1))

def write_html(lines, d, ts):
    # locate target lines by content (resilient to HTML edits shifting line numbers)
    di = _find(lines, lambda l: l.startswith("const DATA = "), "DATA blob")
    lines[di] = "const DATA = " + json.dumps(d, separators=(",", ":"), ensure_ascii=True) + ";\n"
    n = d["summary"]["total_leads"]
    hi = _find(lines, lambda l: '<span class="live-tag">' in l, "header live-tag")
    lines[hi] = re.sub(r'(fresh-tag">)\d+ leads loaded(<)', rf"\g<1>{n} leads loaded\2", lines[hi])
    lines[hi] = re.sub(r'(<span class="live-tag">)[^<]*(</span>)', rf"\g<1>{ts}\2", lines[hi])
    ti = _find(lines, lambda l: "<title>" in l, "title")          # browser-tab lead count
    lines[ti] = re.sub(r"· [\d,]+ leads</title>", f"· {n} leads</title>", lines[ti])
    fi = _find(lines, lambda l: re.search(r"Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", l), "footer Generated")
    lines[fi] = re.sub(r"Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", f"Generated {ts}", lines[fi])
    open(HTML_PATH, "w").writelines(lines)
    open(VERSION_PATH, "w").write(ts + "\n")   # for the page's auto-freshness check

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
    prev_leads = json.dumps(d["leads"], sort_keys=True, ensure_ascii=False)
    prev_holds = json.dumps(d.get("doctrine", {}).get("holds", []), ensure_ascii=False)
    prev_campaigns = json.dumps(d.get("campaigns", []), sort_keys=True, ensure_ascii=False)
    d.setdefault("doctrine", {})["holds"] = [dict(h) for h in CANONICAL_HOLDS]  # enforce canonical holds
    # ---- performance + auto-discover any new FP_S1 campaigns from Instantly
    analytics = fetch_analytics()
    discover_campaigns(d, fetch_campaigns(), analytics)
    campaigns = d["campaigns"]
    id_to_name = {c["id"]: c["name"] for c in campaigns}
    fp_ids = set(id_to_name)
    update_performance(d["performance"], analytics, campaigns)

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
        # attach live per-lead engagement so the dashboard can show who opened
        rec["opens"] = ll.get("email_open_count", 0) or 0
        rec["replies"] = ll.get("email_reply_count", 0) or 0
        rec["clicks"] = ll.get("email_click_count", 0) or 0
        rec["sent_step"] = emails_sent_step(ll)        # emails sent so far (E1..EN)
        rec["lead_status"] = ll.get("status", 0)       # 1=active 3=completed 4=stopped etc.
        roster.append(rec)
        live_rows.append((rec, ll.get("email_open_count", 0) or 0,
                          ll.get("email_reply_count", 0) or 0, ll.get("email_click_count", 0) or 0))
    d["leads"] = roster
    recompute_summary(d["summary"], roster)
    sync_campaign_leadcounts(d["performance"], roster)   # per-campaign counts from roster, not lagging analytics

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rebuild_signal_activity(d["signal_activity"], live_rows, ts)

    # ---- guardrails
    fails = validate(d, prev_total)
    if fails:
        print("VALIDATION_FAILED: " + "; ".join(fails))
        sys.exit(2)

    # ---- change detection (count + performance + per-lead roster content + holds)
    leads_unchanged = json.dumps(d["leads"], sort_keys=True, ensure_ascii=False) == prev_leads
    holds_unchanged = json.dumps(d["doctrine"]["holds"], ensure_ascii=False) == prev_holds
    camps_unchanged = json.dumps(d.get("campaigns", []), sort_keys=True, ensure_ascii=False) == prev_campaigns
    if (leads_unchanged and holds_unchanged and camps_unchanged
            and d["performance"]["totals"] == prev_perf["totals"]
            and d["performance"]["rates"] == prev_perf["rates"]
            and d["performance"]["per_campaign"] == prev_perf.get("per_campaign")):
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
