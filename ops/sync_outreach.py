#!/usr/bin/env python3
"""Sync outreach data (Smartlead email via API, HeyReach LinkedIn via MCP JSON dumps) into Supabase.

Usage:
  python3 sync_outreach.py                 # Smartlead API + HeyReach from data/heyreach/*.json
  python3 sync_outreach.py --skip-smartlead
  python3 sync_outreach.py --skip-heyreach
Env: SUPABASE_SERVICE_KEY (required), GEMINI_API_KEY (optional, else rule-based sentiment).
Secrets: /workspace/.secrets/smartlead_api_key. Never print or commit keys.
See SYNC.md.
"""
import os, re, sys, json, html, argparse, datetime as dt
from collections import defaultdict
from urllib.parse import urlparse
import requests

SB_URL = os.environ.get("SUPABASE_URL", "https://vsxgszlevcygijgulnzm.supabase.co")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SL_BASE = "https://server.smartlead.ai/api/v1"
SL_KEY_FILE = "/workspace/.secrets/smartlead_api_key"
HR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "heyreach")
GEMINI_MODEL = "gemini-3.1-flash-lite"
H = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
NOW = dt.datetime.now(dt.timezone.utc).isoformat()

# ---------------- Supabase helpers ----------------
def upsert(table, rows, on_conflict="id", ignore=False):
    rows = list(rows)
    for i in range(0, len(rows), 500):
        r = requests.post(f"{SB_URL}/rest/v1/{table}?on_conflict={on_conflict}", headers={**H,
            "Prefer": ("resolution=ignore-duplicates" if ignore else "resolution=merge-duplicates") + ",return=minimal"},
            data=json.dumps(rows[i:i+500], default=str), timeout=120)
        if not r.ok: raise RuntimeError(f"upsert {table}: {r.status_code} {r.text[:400]}")
    print(f"  upserted {len(rows)} -> {table}")

def select(table, q):
    r = requests.get(f"{SB_URL}/rest/v1/{table}?{q}", headers=H, timeout=60); r.raise_for_status(); return r.json()

def rpc(fn, args=None):
    r = requests.post(f"{SB_URL}/rest/v1/rpc/{fn}", headers=H, data=json.dumps(args or {}), timeout=120)
    if not r.ok: raise RuntimeError(f"rpc {fn}: {r.status_code} {r.text[:300]}")
    return r.json() if r.text else None

# ---------------- text + sentiment ----------------
def clean(body):
    t = re.sub(r"(?is)<(style|script|head).*?</\1>", " ", body or "")
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    # drop quoted previous message
    t = re.split(r"\n\s*On .{5,200}wrote:|\n-{2,}\s*Original Message|\nFrom: .*\nSent: ", t)[0]
    t = re.sub(r"[ \t\r\xa0]+", " ", t); t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()

OOO = r"out of (the )?office|\bpto\b|out at a conference|\babsente?\b|je serai absent|no longer with|i am out|i'?m out|ooo\b|on (annual |parental |maternity )?leave|vacation|away from (the )?office|limited access to email|auto.?reply|automatic reply|currently out|be back on|return(ing)? on"
NEG = r"^\s*remove\.?\s*$|not applicable|not interested|no thanks|no thank you|unsubscribe|remove me|stop (emailing|contacting)|take me off|don'?t (contact|email)|not a fit|not relevant|we'?re good|pass\b|no need|please don'?t"
POS = r"\bsure\b|send (it|them|this)? ?over|please share|feel free|interested|sounds (good|great)|let'?s (chat|talk|connect)|book|calendar|would love|yes\b|go ahead|happy to (look|take)|share (it|the)|take a look|keen"
def rule_sentiment(t):
    s = (t or "").lower()
    if re.search(OOO, s): return "ooo"
    if re.search(NEG, s): return "negative"
    if re.search(POS, s): return "positive"
    return "neutral"

GEMINI_OK = True
def gemini_sentiment(t):
    global GEMINI_OK
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not GEMINI_OK: return None
    prompt = ("Classify this reply to a B2B cold outreach message (we offered to send a free list of target accounts). "
              "Answer with exactly one word: positive (wants it / interested / agrees), neutral (unclear, question, greeting), "
              "negative (not interested / unsubscribe / referral away), ooo (auto-reply / out of office).\n\nReply:\n" + (t or "")[:3000])
    try:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                          params={"key": key}, json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0}}, timeout=60)
        r.raise_for_status()
        out = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip().lower()
        for k in ("positive", "neutral", "negative", "ooo"):
            if k in out: return k
    except Exception as e:
        print("  gemini failed, falling back to rules for this run:", str(e)[:80]); GEMINI_OK = False
    return None

NDR = r"address not found|message blocked|email address error|delivery status notification|undeliverable|mail delivery (failed|subsystem)|wasn'?t delivered"
def is_bounce(t): return bool(re.search(NDR, (t or "")[:400].lower()))

def classify(t):
    if re.search(OOO, (t or "").lower()): return "ooo", "rules"
    g = gemini_sentiment(t)
    return (g, "gemini") if g else (rule_sentiment(t), "rules")

def existing_replies():
    return {r["id"]: r for r in select("replies", "select=id,sentiment,sentiment_source,sentiment_manual,message_text&limit=10000")}

def with_sentiment(rows):
    ex = existing_replies()
    for r in rows:
        e = ex.get(r["id"])
        if e and (e["sentiment_manual"] or (e["sentiment"] and e["message_text"] == r["message_text"])):
            r["sentiment"], r["sentiment_source"] = e["sentiment"], e["sentiment_source"]  # keep prior/manual label
        else:
            r["sentiment"], r["sentiment_source"] = classify(r["message_text"])
    return rows

def ist_day(ts):
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (t + dt.timedelta(hours=5, minutes=30)).date().isoformat()

def domain_of(x):
    if not x: return None
    x = x.strip().lower()
    if "@" in x: x = x.split("@")[-1]
    if "//" not in x: x = "//" + x
    d = urlparse(x).netloc.split(":")[0]
    return d[4:] if d.startswith("www.") else (d or None)

# ---------------- Smartlead ----------------
def sl_get(path, **q):
    key = open(SL_KEY_FILE).read().strip()
    r = requests.get(SL_BASE + path, params={**q, "api_key": key}, timeout=90)
    if not r.ok: raise RuntimeError(f"smartlead {path}: {r.status_code}")
    return r.json()

def sync_smartlead():
    print("Smartlead …")
    camps, leads, reps, daily = [], [], [], defaultdict(lambda: [0, 0]); bdaily = defaultdict(int)
    for c in sl_get("/campaigns"):
        cid = c["id"]; key = f"smartlead:{cid}"
        a = sl_get(f"/campaigns/{cid}/analytics")
        stats = []; o = 0
        while True:
            p = sl_get(f"/campaigns/{cid}/statistics", limit=500, offset=o); stats += p["data"]; o += 500
            if o >= int(p.get("total_stats") or 0) or not p["data"]: break
        ll = []; o = 0
        while True:
            p = sl_get(f"/campaigns/{cid}/leads", limit=100, offset=o); ll += p["data"]; o += 100
            if o >= int(p.get("total_leads") or 0) or not p["data"]: break
        by_email = defaultdict(list)
        for s in stats: by_email[(s["lead_email"] or "").lower()].append(s)
        for s in stats:
            if s.get("sent_time"):
                daily[ist_day(s["sent_time"])][0] += 1
                if s.get("is_bounced"): bdaily[ist_day(s["sent_time"])] += 1
        for x in ll:
            L = x["lead"]; em = (L.get("email") or "").lower(); ss = by_email.get(em, [])
            sent = sorted(s["sent_time"] for s in ss if s.get("sent_time"))
            replied = any(s.get("reply_time") for s in ss); bounced = any(s.get("is_bounced") for s in ss)
            lid = f"email:{cid}:{em}"
            leads.append(dict(id=lid, channel="email", campaign_id=key,
                person_name=" ".join(filter(None, [L.get("first_name"), L.get("last_name")])) or None,
                email=em or None, linkedin_url=L.get("linkedin_profile"), company_name=L.get("company_name"),
                company_domain=domain_of(L.get("website")) or domain_of(em), status=x.get("status"),
                contacted=bool(sent), contacted_at=sent[0] if sent else None, bounced=bounced, replied=replied,
                messaged=bool(sent), first_messaged_at=sent[0] if sent else None,
                bounced_at=min([s["sent_time"] for s in ss if s.get("is_bounced") and s.get("sent_time")] or [None]) if bounced else None,
                raw={"smartlead_lead_id": L["id"], "lead_category_id": x.get("lead_category_id"), "custom_fields": L.get("custom_fields")},
                updated_at=NOW))
            if replied:
                try:
                    hist = sl_get(f"/campaigns/{cid}/leads/{L['id']}/message-history").get("history", [])
                except Exception as e:
                    print("  message-history failed for a lead:", e); hist = []
                thread = []
                for m in hist:
                    thread.append({"from": "me" if m.get("type") == "SENT" else "them", "at": m.get("time"),
                                   "subject": m.get("subject"), "body": clean(m.get("email_body"))[:20000]})
                    if m.get("type") != "REPLY": continue
                    txt = clean(m.get("email_body"))
                    reps.append(dict(id=f"sl:{m.get('message_id') or m.get('stats_id')+m.get('time','')}", channel="email",
                        campaign_id=key, lead_id=lid, person_name=leads[-1]["person_name"], email=em,
                        linkedin_url=L.get("linkedin_profile"), company_name=L.get("company_name"),
                        company_domain=leads[-1]["company_domain"], subject=m.get("subject"),
                        message_text=txt[:50000], received_at=m.get("time"), raw={"stats_id": m.get("stats_id")},
                        thread=list(thread), updated_at=NOW))
                    if m.get("time"): daily[ist_day(m["time"])][1] += 1
        camps.append(dict(id=key, channel="email", external_id=str(cid), name=c["name"], status=c["status"],
            leads_total=int(a.get("campaign_lead_stats", {}).get("total") or len(ll)),
            contacted=int(a.get("unique_sent_count") or 0), sent=int(a.get("sent_count") or 0),
            replies=int(a.get("reply_count") or 0), bounces=int(a.get("bounce_count") or 0),
            stats=a, created_at=c.get("created_at"), synced_at=NOW))
    reps = with_sentiment([r for r in reps if r["received_at"] and not is_bounce(r["message_text"])])
    pos = defaultdict(int)
    for r in reps:
        if r["sentiment"] == "positive": pos[r["campaign_id"]] += 1
    for c in camps: c["positives"] = pos[c["id"]]
    upsert("outreach_campaigns", camps); upsert("outreach_leads", leads); upsert("replies", reps)
    upsert("outreach_daily", [dict(day=d, channel="email", touches=v[0], sends=v[0], bounces=bdaily[d], replies=v[1]) for d, v in daily.items() if d], "day,channel")
    upsert("outreach_sync_runs", [dict(source="smartlead", synced_at=NOW, details={"campaigns": len(camps), "leads": len(leads), "replies": len(reps)})], ignore=True)
    return len(reps)

# ---------------- HeyReach (JSON dumped by an agent via MCP) ----------------
def norm_li(u):
    if not u: return None
    u = re.sub(r"^https?://(www\.)?", "", u.strip().lower()).rstrip("/")
    return "https://www." + u + "/"

def load(name):
    p = os.path.join(HR_DIR, name)
    return json.load(open(p)) if os.path.exists(p) else None

def sync_heyreach():
    print("HeyReach (from", HR_DIR, ") …")
    camps_j, leads_j, conv_j, stats_j = load("campaigns.json"), load("leads.json") or {}, load("conversations.json"), load("overall_stats.json")
    if not camps_j: print("  no campaigns.json – skip"); return 0
    # first message we sent to each profile, from conversations
    first_msg = {}
    for c in (conv_j or {}).get("items", []):
        u = norm_li((c.get("correspondentProfile") or {}).get("profileUrl"))
        mine = sorted(m["createdAt"] for m in (c.get("messages") or []) if m.get("sender") == "ME" and m.get("createdAt"))
        if u and mine: first_msg[u] = min(mine[0], first_msg.get(u, mine[0]))
    leads, li_index = [], {}
    per = defaultdict(lambda: dict(contacted=0, replies=0, failed=0))
    for cid, items in leads_j.items():
        for x in items:
            p = x.get("linkedInUserProfile") or {}; url = norm_li(p.get("profileUrl"))
            if not url: continue
            contacted = x.get("leadConnectionStatus") not in (None, "None") or x.get("leadMessageStatus") not in (None, "None")
            replied = x.get("leadMessageStatus") == "MessageReply"
            messaged = x.get("leadMessageStatus") in ("MessageSent", "MessageReply") or url in first_msg
            fm = first_msg.get(url) or ((x.get("lastActionTime") or x.get("finishedTime") or x.get("creationTime")) if messaged else None)
            lid = f"linkedin:{cid}:{url}"
            leads.append(dict(id=lid, channel="linkedin", campaign_id=f"heyreach:{cid}",
                person_name=" ".join(filter(None, [p.get("firstName"), p.get("lastName")])) or None, title=p.get("position") or p.get("headline"),
                email=p.get("emailAddress") or p.get("enrichedEmailAddress"), linkedin_url=url, company_name=p.get("companyName"),
                status=f'{x.get("leadCampaignStatus")} / {x.get("leadConnectionStatus")} / {x.get("leadMessageStatus")}',
                contacted=contacted, contacted_at=(x.get("lastActionTime") or x.get("creationTime")) if contacted else None,
                replied=replied, messaged=messaged, first_messaged_at=fm,
                connection_accepted=x.get("leadConnectionStatus") == "ConnectionAccepted",
                raw={"heyreach_lead_id": x.get("id"), "error": x.get("errorCode")}, updated_at=NOW))
            per[str(cid)]["contacted"] += contacted; per[str(cid)]["replies"] += replied
            if x.get("leadCampaignStatus") == "Failed": per[str(cid)]["failed"] += 1
            # prefer most recent campaign for a person
            if url not in li_index or (x.get("creationTime") or "") > li_index[url][1]: li_index[url] = (lid, x.get("creationTime") or "")
    reps = []
    for c in (conv_j or {}).get("items", []):
        p = c.get("correspondentProfile") or {}; url = norm_li(p.get("profileUrl"))
        lid = li_index.get(url, (None,))[0]
        name = " ".join(filter(None, [p.get("firstName"), p.get("lastName")]))
        for i, m in enumerate(c.get("messages") or []):
            if m.get("sender") == "ME": continue
            reps.append(dict(id=f"hr:{c['id']}:{m.get('createdAt')}", channel="linkedin",
                campaign_id=lid.split(":")[0] + ":" + lid.split(":")[1] if lid else None, lead_id=lid, person_name=name,
                linkedin_url=url, company_name=p.get("companyName"), message_text=(m.get("body") or "").strip()[:50000],
                title=p.get("position") or p.get("headline"), email=p.get("emailAddress") or p.get("enrichedEmailAddress"),
                thread=[{"from": "me" if mm.get("sender") == "ME" else "them", "at": mm.get("createdAt"), "body": mm.get("body")}
                        for mm in (c.get("messages") or [])[:i + 1]],
                received_at=m.get("createdAt"), raw={"conversation_id": c["id"], "headline": p.get("headline")}, updated_at=NOW))
    for r in reps:
        if r["campaign_id"]: r["campaign_id"] = "heyreach:" + r["lead_id"].split(":")[1]
    reps = with_sentiment(reps)
    pos = defaultdict(int)
    for r in reps:
        if r["sentiment"] == "positive" and r["campaign_id"]: pos[r["campaign_id"]] += 1
    camps = []
    for c in camps_j.get("items", []):
        ps = c.get("progressStats") or {}; k = str(c["id"])
        camps.append(dict(id=f"heyreach:{k}", channel="linkedin", external_id=k, name=c["name"], status=c["status"],
            leads_total=ps.get("totalUsers", 0), contacted=per[k]["contacted"], sent=per[k]["contacted"],
            replies=per[k]["replies"], positives=pos[f"heyreach:{k}"], bounces=0, stats=ps,
            created_at=c.get("creationTime"), synced_at=NOW))
    upsert("outreach_campaigns", camps)
    ids = {c["id"] for c in camps}
    upsert("outreach_leads", [l for l in leads if l["campaign_id"] in ids])
    upsert("replies", reps)
    if stats_j:
        d = [dict(day=k[:10], channel="linkedin",
                  touches=v.get("connectionsSent", 0) + v.get("totalMessageStarted", 0) + v.get("messagesSent", 0) + v.get("inmailMessagesSent", 0),
                  conn_requests=v.get("connectionsSent", 0), conn_accepted=v.get("connectionsAccepted", 0),
                  messages_sent=v.get("totalMessageStarted", 0) + v.get("messagesSent", 0) + v.get("inmailMessagesSent", 0),
                  replies=v.get("totalMessageReplies", 0) + v.get("totalInmailReplies", 0)) for k, v in stats_j.get("byDayStats", {}).items()]
        upsert("outreach_daily", d, "day,channel")
    upsert("outreach_sync_runs", [dict(source="heyreach", synced_at=NOW, details={"campaigns": len(camps), "leads": len(leads), "replies": len(reps),
            "overall": (stats_j or {}).get("overallStats")})], ignore=True)
    return len(reps)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--skip-smartlead", action="store_true"); ap.add_argument("--skip-heyreach", action="store_true")
    a = ap.parse_args()
    if not a.skip_smartlead: sync_smartlead()
    if not a.skip_heyreach: sync_heyreach()
    rpc("link_outreach"); rpc("link_reply_titles"); n = rpc("ensure_deals")
    print("deals created this run:", n)
