#!/usr/bin/env python3
"""
ltc.py — Ontario MLTC long-term-care inspection fetcher + comparison tool.

Pulls the Ministry of Long-Term Care's public inspection reports through the
reporting site's public guest API, caches everything in SQLite, and renders a
self-contained HTML site: an overview dashboard comparing homes over time, plus
a per-home dashboard (trends, finding-type mix, the actual findings, fine
details). Pure standard library — no dependencies.

Data source:  https://publicreports.mltc.gov.on.ca
API contract: docs/API.md   ·   methodology: docs/methodology.md

Usage:
  python3 ltc.py fetch --cities Kitchener,Waterloo,Cambridge,Galt
  python3 ltc.py fetch --geo geo/ontario.txt        # whole-Ontario sweep
  python3 ltc.py fetch --homes 2917
  python3 ltc.py reparse                            # re-parse cached raw (no network)
  python3 ltc.py report --out data/report           # render the HTML site + CSVs
  python3 ltc.py status
"""
import argparse
import csv
import html as _h
import json
import re
import random
import sqlite3
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

API_URL = ("https://publicreports.mltc.gov.on.ca/webruntime/api/apex/execute"
          "?language=en-CA&asGuest=true&htmlEncode=false")
CLASSNAME = "@udd/01pOH000000GbLN"          # Salesforce Apex class id (can change on redeploy)
BASE = Path(__file__).resolve().parent
DB = BASE / "ltc.db"
DELAY = 0.25
REGION_CITIES = ["Kitchener", "Waterloo", "Cambridge", "Galt"]
HOME_URL = "https://publicreports.mltc.gov.on.ca/reports?home={hn}&guestUserLang=en-CA"
REPORT_URL = "https://publicreports.mltc.gov.on.ca/reportDetails?detailId={iid}&homeId={hn}&guestUserLang=en-CA"


# ---------------------------------------------------------------- HTTP / API
def api(method, params, classname=CLASSNAME, retries=4, timeout=40):
    payload = {"namespace": "", "classname": classname, "method": method,
               "isContinuation": False, "params": params, "cacheable": False}
    data = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API_URL, data=data, method="POST", headers={
                "Content-Type": "application/json",
                "Origin": "https://publicreports.mltc.gov.on.ca",
                "Referer": "https://publicreports.mltc.gov.on.ca/",
                "User-Agent": "Mozilla/5.0 (ltc-inspections)",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("api %s failed after %d tries: %s" % (method, retries, last))


def search_homes(term):
    r = api("getHomeDetailsbyNameOrLocarion", {"homeNameOrLocation": term})
    return r.get("returnValue") or []


def home_report_list(home_number):
    r = api("getHomeDetails", {"accountNumber": str(home_number)})
    rl = (r.get("returnValue") or {}).get("reportList")
    return json.loads(rl) if rl else []


def report_detail(inspection_id, home_number):
    r = api("getHomeInspectionDetails",
            {"inspectionId": inspection_id, "homeId": str(home_number), "usePublish": True})
    return (r.get("returnValue") or {}).get("inspectionDetails") or {}


# ---------------------------------------------------------------- parsing
def clean(t):
    if not t:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|li|div|h\d|span|strong|ul)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _h.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


FINDING_HEAD = re.compile(r"WRITTEN NOTIFICATION|NOTICE OF ADMINISTRATIVE MONETARY PENALTY")


def parse_findings(wn_html):
    """Split a report's WN_Details HTML into structured finding/fine records.

    Handles both known markup eras (h2 headers and strong headers).
    Returns a list of dicts: seq, type (WN|AMP), title, category, nc,
    statute, fine, source, text.
    """
    wn = wn_html or ""
    markers = [(m.start(), "WN" if m.group(0) == "WRITTEN NOTIFICATION" else "AMP")
               for m in FINDING_HEAD.finditer(wn)]
    markers.sort()
    out = []
    for i, (p, kind) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(wn)
        block = wn[wn.rfind("<", 0, p):end]
        ncm = re.search(r"NC\s*#\s*(\d*)", block)
        nc = ncm.group(1) if ncm else ""
        statm = re.search(r"Non-compliance with:\s*([^<]+?)\s*(?:</|<)", block, re.I)
        statute = clean(statm.group(1)).strip() if statm else ""
        finem = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", block)
        fine = float(finem.group(1).replace(",", "")) if finem else 0.0
        srcre = re.search(r"Sources?:\s*(.+?)(?:<br|<br/>|$)", block, re.I | re.S)
        source = clean(srcre.group(1)).strip() if srcre else ""
        plain = clean(block)
        lines = [l.strip() for l in plain.split("\n") if l.strip()]
        body, skip = [], False
        for l in lines:
            if kind == "WN" and l.lower().startswith("written notification:"):
                continue
            if kind == "AMP" and l.lower().startswith("notice of administrative monetary penalty"):
                continue
            if re.match(r"^NC\s*#?\d*\s", l, re.I):
                continue
            if re.match(r"^(Written|Compliance)\s+Notification\s+pursuant", l, re.I):
                continue
            if l.lower().startswith("non-compliance with:"):
                continue
            if re.match(r"^(s\.\s*\d|O\.\s*Reg\.|Pursuant to)", l, re.I):
                continue
            if re.match(r"^(Related to Written Notification|Compliance History|Invoice with payment|"
                        r"Licensees must not pay|In accordance with s\.\s*349|This is the first AMP|"
                        r"The administrative penalty)", l, re.I):
                continue
            if re.match(r"^Sources?:\s", l, re.I):
                skip = True
                continue
            if skip:
                continue
            body.append(l)
        title = lines[0] if lines else ""
        if title.lower().startswith("written notification:"):
            title = title.split(":", 1)[1].strip()
        category = "Administrative Monetary Penalty" if kind == "AMP" else (title or "Written Notification")
        out.append({"seq": len(out) + 1, "type": kind, "title": title, "category": category,
                    "nc": nc, "statute": statute, "fine": fine, "source": source,
                    "text": " ".join(body).strip()})
    return out


def parse_detail(det):
    wn = det.get("WN_Details__c") or ""
    complied = det.get("Complied__c") or ""
    protocols_raw = clean(det.get("Inspection_Protocols__c") or "")
    prot_lines = [l.strip() for l in protocols_raw.splitlines() if l.strip()]
    protocols = [l for l in prot_lines if not re.search(r"Inspection Protocols|were used|protocoles", l, re.I)]
    findings = parse_findings(wn)
    return {
        "findings": findings,
        "wn_count": sum(1 for f in findings if f["type"] == "WN"),
        "amp_total": sum(f["fine"] for f in findings),
        "co_related": len(re.findall(r"Compliance Order", wn, re.I)),
        "orders_in_compliance": len(re.findall(r"Order\s*#\d+", complied)),
        "intake_type": re.sub(r"\s+", " ", clean(det.get("Intake_Type__c") or "")).strip(),
        "protocols": protocols,
    }


# ---------------------------------------------------------------- database
def db():
    c = sqlite3.connect(str(DB))
    c.executescript("""
    CREATE TABLE IF NOT EXISTS homes(
        home_number TEXT PRIMARY KEY, name TEXT, city TEXT, licensee TEXT, geo TEXT);
    CREATE TABLE IF NOT EXISTS inspections(
        inspection_id TEXT PRIMARY KEY, home_number TEXT, home_name TEXT, city TEXT,
        licensee TEXT, inspection_number TEXT, date_issued TEXT, report_type TEXT,
        intake_type TEXT, wn_count INT, amp_total REAL, co_related INT,
        orders_in_compliance INT, protocols TEXT, raw TEXT, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS findings(
        finding_id TEXT PRIMARY KEY, inspection_id TEXT, home_number TEXT, seq INT,
        type TEXT, category TEXT, title TEXT, nc TEXT, statute TEXT, fine REAL,
        source TEXT, text TEXT);
    CREATE INDEX IF NOT EXISTS ix_findings_home ON findings(home_number);
    CREATE INDEX IF NOT EXISTS ix_findings_insp ON findings(inspection_id);
    """)
    return c


def upsert_home(c, h, geo):
    c.execute("INSERT INTO homes(home_number,name,city,licensee,geo) VALUES(?,?,?,?,?) "
              "ON CONFLICT(home_number) DO UPDATE SET "
              "name=COALESCE(excluded.name,name), city=COALESCE(excluded.city,city), "
              "licensee=COALESCE(excluded.licensee,licensee)",
              (str(h.get("Home_Number__c")), h.get("Home_Name__c"), h.get("Home_City__c"),
               h.get("Licensee__c"), geo))


def upsert_report_meta(c, rep, home):
    c.execute("""
    INSERT INTO inspections(inspection_id,home_number,home_name,city,licensee,
        inspection_number,date_issued,report_type)
    VALUES(?,?,?,?,?,?,?,?)
    ON CONFLICT(inspection_id) DO UPDATE SET
        home_name=excluded.home_name, city=excluded.city, licensee=excluded.licensee,
        inspection_number=excluded.inspection_number, date_issued=excluded.date_issued,
        report_type=excluded.report_type""",
        (rep["Id"], str(home.get("Home_Number__c")), home.get("Home_Name__c"),
         home.get("Home_City__c"), home.get("Licensee__c"),
         rep.get("Inspection_Number__c"), rep.get("Date_LR_Issued__c"), rep.get("Report_Type__c")))


def store_detail(c, inspection_id, home_number, raw, parsed):
    c.execute("""
    INSERT INTO inspections(inspection_id,intake_type,wn_count,amp_total,co_related,
        orders_in_compliance,protocols,raw,fetched_at)
    VALUES(?,?,?,?,?,?,?,?,datetime('now'))
    ON CONFLICT(inspection_id) DO UPDATE SET
        intake_type=excluded.intake_type, wn_count=excluded.wn_count, amp_total=excluded.amp_total,
        co_related=excluded.co_related, orders_in_compliance=excluded.orders_in_compliance,
        protocols=excluded.protocols, raw=excluded.raw, fetched_at=excluded.fetched_at""",
        (inspection_id, parsed["intake_type"], parsed["wn_count"], parsed["amp_total"],
         parsed["co_related"], parsed["orders_in_compliance"], json.dumps(parsed["protocols"]),
         json.dumps(raw)))
    c.execute("DELETE FROM findings WHERE inspection_id=?", (inspection_id,))
    for f in parsed["findings"]:
        c.execute("INSERT INTO findings(finding_id,inspection_id,home_number,seq,type,category,"
                  "title,nc,statute,fine,source,text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("%s:%d" % (inspection_id, f["seq"]), inspection_id, str(home_number),
                   f["seq"], f["type"], f["category"], f["title"], f["nc"], f["statute"],
                   f["fine"], f["source"], f["text"]))


# ---------------------------------------------------------------- commands
def collect_targets(c, args):
    homes = {}

    def add(term, h):
        hn = str(h.get("Home_Number__c"))
        homes[hn] = {"Home_Number__c": hn, "Home_Name__c": h.get("Home_Name__c"),
                     "Home_City__c": h.get("Home_City__c"), "Licensee__c": h.get("Licensee__c")}
        upsert_home(c, homes[hn], term)

    if args.homes:
        for hn in [x.strip() for x in args.homes.split(",") if x.strip()]:
            # name/city are resolved (throttled) in cmd_fetch; avoid a burst here
            homes[hn] = {"Home_Number__c": hn, "Home_Name__c": None, "Home_City__c": None, "Licensee__c": None}
            upsert_home(c, homes[hn], "homes")
    else:
        if args.geo:
            terms = [ln.strip() for ln in Path(args.geo).read_text().splitlines()
                     if ln.strip() and not ln.startswith("#")]
        else:
            terms = [x.strip() for x in (args.cities.split(",") if args.cities else REGION_CITIES) if x.strip()]
        for term in terms:
            print("[search] %s" % term, flush=True)
            for h in search_homes(term):
                add(term, h)
            time.sleep(DELAY)
    c.commit()
    return [homes[k] for k in homes]


def cmd_fetch(args):
    c = db()
    targets = collect_targets(c, args)
    delay = float(getattr(args, "delay", DELAY))
    print("Homes to process: %d (detail delay ~%.2fs)" % (len(targets), delay), flush=True)
    empty_streak = 0
    for home in targets:
        hn = str(home["Home_Number__c"])
        reps = home_report_list(hn)
        time.sleep(min(delay, 0.6))
        # An empty list for a known home is usually guest-API throttling, not
        # "no data": retry with backoff, and pause longer on a run of empties.
        tries = 0
        while not reps and tries < 2:
            tries += 1
            wait = 5 * tries
            print("    [retry] %s empty, backoff %ds (try %d)" % (hn, wait, tries), flush=True)
            time.sleep(wait)
            reps = home_report_list(hn)
        if not reps:
            empty_streak += 1
            if empty_streak >= 5:
                print("    [throttle] %d empties in a row - pausing 90s" % empty_streak, flush=True)
                time.sleep(90)
                empty_streak = 0
                reps = home_report_list(hn)
        else:
            empty_streak = 0
        if reps:
            home["Home_Name__c"] = home.get("Home_Name__c") or reps[0].get("Home_Name__c")
            home["Home_City__c"] = home.get("Home_City__c") or reps[0].get("Home_City__c")
        for rep in reps:
            upsert_report_meta(c, rep, home)
        c.commit()
        print("[home] %s (%s): %d reports" % (home.get("Home_Name__c"), hn, len(reps)), flush=True)
        for rep in reps:
            iid = rep["Id"]
            if not args.force and c.execute(
                    "SELECT 1 FROM inspections WHERE inspection_id=? AND fetched_at IS NOT NULL",
                    (iid,)).fetchone():
                continue
            time.sleep(delay * (0.8 + 0.4 * random.random()))
            raw = report_detail(iid, hn)
            store_detail(c, iid, hn, raw, parse_detail(raw))
            c.commit()
            print("    %s %s" % (rep.get("Date_LR_Issued__c"), rep.get("Inspection_Number__c")), flush=True)
    c.close()
    print("\nFetch complete. Run: python3 ltc.py report", flush=True)


def cmd_reparse(args):
    c = db()
    rows = c.execute("SELECT inspection_id, home_number, raw FROM inspections WHERE fetched_at IS NOT NULL").fetchall()
    city_map, lic_map = {}, {}
    for iid, hn, raw in rows:
        det = json.loads(raw)
        store_detail(c, iid, hn, raw, parse_detail(det))
        if det.get("Home_City__c"):
            city_map[hn] = det["Home_City__c"]
        if det.get("Licensee__c"):
            lic_map[hn] = det["Licensee__c"]
    c.commit()
    for hn, v in city_map.items():
        c.execute("UPDATE homes SET city=? WHERE home_number=?", (v, hn))
    for hn, v in lic_map.items():
        c.execute("UPDATE homes SET licensee=? WHERE home_number=?", (v, hn))
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM findings WHERE type='WN'").fetchone()[0]
    print("Re-parsed %d reports -> %d findings" % (len(rows), n))


def cmd_status(args):
    c = db()
    h = c.execute("SELECT COUNT(*) FROM homes").fetchone()[0]
    i = c.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]
    f = c.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    print("Homes: %d  Inspections: %d  Findings: %d" % (h, i, f))
    for r in c.execute("SELECT home_number,name,city FROM homes ORDER BY name"):
        print("  %s  %s  (%s)" % (r[0], r[1], r[2]))


# ---------------------------------------------------------------- aggregation
def load(c):
    c.row_factory = sqlite3.Row
    homes = [dict(r) for r in c.execute("SELECT * FROM homes")]
    insp = [dict(r) for r in c.execute("SELECT * FROM inspections WHERE fetched_at IS NOT NULL")]
    findings = [dict(r) for r in c.execute(
        "SELECT f.*, i.date_issued, i.intake_type, i.home_name, i.inspection_number, "
        "i.amp_total AS report_amp FROM findings f "
        "JOIN inspections i ON i.inspection_id=f.inspection_id ORDER BY i.date_issued DESC")]
    return homes, insp, findings


def esc(s):
    return _h.escape(str(s if s is not None else ""))


def home_file(hn):
    return "home_%s.html" % re.sub(r"[^A-Za-z0-9]", "_", str(hn))


# ---------------------------------------------------------------- main dashboard
def load_city_region():
    p = BASE / "geo" / "city_region.txt"
    m = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "|" in line:
                k, _, v = line.partition("|")
                if k:
                    m[k] = v
    return m


def render_dashboard(homes, insp, findings, region_label):
    by = defaultdict(lambda: dict(insp=0, wn=0, amp=0.0, co=0, byyear={}, first=None, last=None))
    for r in insp:
        b = by[r["home_number"]]
        b["insp"] += 1
        b["wn"] += r["wn_count"] or 0
        b["amp"] += r["amp_total"] or 0
        b["co"] += r["co_related"] or 0
        y = (r["date_issued"] or "")[:4] or "?"
        b["byyear"][y] = b["byyear"].get(y, 0) + (r["wn_count"] or 0)
        d = r["date_issued"]
        if d:
            b["first"] = d if not b["first"] or d < b["first"] else b["first"]
            b["last"] = d if not b["last"] or d > b["last"] else b["last"]
    year_set = sorted({y for b in by.values() for y in b["byyear"] if y != "?"})
    home_name = {h["home_number"]: h["name"] for h in homes}
    home_city = {h["home_number"]: (h["city"] or "") for h in homes}
    home_lic = {h["home_number"]: (h["licensee"] or "") for h in homes}

    total_nc = sum(b["wn"] for b in by.values())
    total_amp = sum(b["amp"] for b in by.values())
    total_co = sum(b["co"] for b in by.values())
    total_ins = sum(b["insp"] for b in by.values())
    dmin = min((r["date_issued"] for r in insp if r.get("date_issued")), default="")
    dmax = max((r["date_issued"] for r in insp if r.get("date_issued")), default="")
    cities = sorted({c for c in home_city.values() if c})
    city_region = load_city_region()

    # homes data for the interactive table
    homes_data = []
    for hn, b in by.items():
        homes_data.append({
            "hn": hn, "name": home_name.get(hn, hn), "city": home_city.get(hn, ""),
            "region": city_region.get(home_city.get(hn, ""), "Other"),
            "licensee": home_lic.get(hn, ""), "url": HOME_URL.format(hn=hn),
            "page": home_file(hn), "byyear": b["byyear"], "amp": round(b["amp"], 2),
            "co": b["co"], "insp": b["insp"], "last": b["last"] or "",
        })
    homes_json = json.dumps(homes_data, ensure_ascii=False)
    years_json = json.dumps(year_set)

    # default server-rendered table rows (total findings desc) for no-JS fallback
    rows_html = "".join(
        "<tr data-hn='%s'><td class='name'><a href='%s'>%s</a><div class='sub'>%s</div></td>"
        "<td class='num citycell'>%s</td><td class='num strong'>%d</td><td class='num'>$%s</td>"
        "<td class='num'>%d</td><td class='num'>%d</td><td class='num'>%s</td></tr>"
        % (esc(h["hn"]), esc(h["page"]), esc(h["name"]), esc(h["city"]), esc(h["city"]),
           sum(h["byyear"].values()), format(int(h["amp"]), ","), h["insp"], h["co"], esc(h["last"]))
        for h in sorted(homes_data, key=lambda x: -sum(x["byyear"].values())))

    template = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='18' fill='%232563eb'/%3E%3Cpath d='M50 30v40M30 50h40' stroke='%23fff' stroke-width='13' stroke-linecap='round'/%3E%3C/svg%3E">
<title>Ontario LTC Inspection Comparison</title>
<style>
:root{--ink:#1a1c24;--muted:#6b7280;--line:#e6e8ee;--find:#e0552b;--zero:#d7dbe4;--bg:#f6f7fa;--accent:#2f6df6}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:24px;line-height:1.45}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;font-weight:400}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;flex:1 1 130px}
.card .n{font-size:26px;font-weight:700}
.card .l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
section{margin:28px 0}
h2{font-size:16px;margin:0 0 6px}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0 12px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.toolbar label{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px}
select,input[type=checkbox]{font-size:13px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink)}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:14px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}
th{background:#fff;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--ink)}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.strong{font-weight:700}
tr:hover td{background:#fafbfe}
.name a{font-weight:600}
.name .sub{margin-top:2px}
.note{font-size:12px;color:var(--muted);margin-top:8px}
.arow{display:flex;align-items:center;gap:10px;margin:7px 0}
.albl{width:300px;font-size:13px}
.atrk{flex:1;background:#eef0f4;border-radius:4px;height:14px}
.abar{background:var(--find);height:14px;border-radius:4px;min-width:2px}
.aval{width:26px;text-align:right;font-variant-numeric:tabular-nums;font-size:13px}
footer{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
details.method{background:#fff;border:1px solid var(--line);border-radius:10px;padding:6px 16px}
details.method summary{cursor:pointer;font-weight:600;padding:8px 0}
details.method p, details.method li{font-size:13px;color:#333}
</style></head>
<body><div class="wrap">
<h1>Ontario LTC Inspection Comparison <span class="sub">— @@REGION@@</span></h1>
<div class="sub">Public inspection findings, Ministry of Long-Term Care · @@DATE_RANGE@@ · generated @@GENERATED@@</div>
<div class="cards">
<div class="card"><div class="n">@@N_HOMES@@</div><div class="l">Homes</div></div>
<div class="card"><div class="n">@@TOTAL_INS@@</div><div class="l">Inspections</div></div>
<div class="card"><div class="n">@@TOTAL_NC@@</div><div class="l">Findings</div></div>
<div class="card"><div class="n">@@TOTAL_CO@@</div><div class="l">CO-related</div></div>
<div class="card"><div class="n">$@@TOTAL_AMP@@</div><div class="l">Fines</div></div>
</div>

<section>
<h2>Homes compared by findings</h2>
<div class="toolbar">
<label>Region <select id="f-region"><option value="">All</option>@@REGION_OPTS@@</select></label>
<label>City <select id="f-city"><option value="">All</option>@@CITY_OPTS@@</select></label>
<label>Years <select id="f-from">@@YEAR_OPTS@@</select> to <select id="f-to">@@YEAR_OPTS@@</select></label>
<label><input type="checkbox" id="f-fines"> Fines only</label>
<span class="note" id="f-count"></span>
</div>
<table id="tbl">
<thead><tr>
<th data-k="name">Home</th><th data-k="city">City</th>
<th data-k="findings" class="num">Findings</th><th data-k="amp" class="num">Fines</th>
<th data-k="insp" class="num">Inspections</th><th data-k="co" class="num">CO-rel</th>
<th data-k="last" class="num">Last</th>
</tr></thead>
<tbody>@@ROWS@@</tbody>
</table>
<div class="note">Click a column to sort. “Findings” sums written-notification findings across the selected year range. “Fines” = administrative monetary penalties. “CO-rel” = findings that reference a compliance order. Click a home name for its dashboard.</div>
</section>

<details class="method">
<summary>Methodology & data sources</summary>
<p><b>Data source.</b> Every figure comes from the Ontario Ministry of Long-Term Care's public inspection
reports at <a href="https://publicreports.mltc.gov.on.ca">publicreports.mltc.gov.on.ca</a>, pulled through the
site's public (unauthenticated) data API — the same data shown on the Ministry's website. Each home's
official report list is linked from its row above; each finding links to its source report.</p>
<p><b>What a “finding” is.</b> A finding is a written notification of non-compliance the Ministry issued in a
published report (a violation of the <i>Fixing Long-Term Care Act, 2021</i> or its regulations). Reports also
may issue a <b>compliance order</b> (a binding directive, the most severe enforcement short of licence action)
and/or an <b>administrative monetary penalty (AMP)</b> — a fine.</p>
<p><b>How it's calculated.</b> For each published report we parse the findings section and record, per finding:
its type, the regulation section cited, the narrative, the stated source, and any fine. A home's “Findings”
count is the number of written-notification findings; “Fines” is the sum of AMP amounts; “CO-rel” counts
findings that reference a compliance order (typically a home that failed to fix a prior order). Counts are by
calendar year of the report issue date.</p>
<p><b>Important caveats.</b> These are findings <i>issued</i>, not their resolved/closed status — a year with
many findings can reflect a serious problem <i>or</i> a thorough inspection that was later remediated (follow-up
reports note when a prior order was found in compliance). The public site covers reports it publishes; very old
records can be requested from the Ministry by email. The comparison is informational, not a ranking of quality
of care.</p>
<p><b>Source links.</b> <a href="https://publicreports.mltc.gov.on.ca">Ministry public reports</a> ·
API contract in <code>docs/API.md</code> · calculation details in <code>docs/methodology.md</code>.</p>
</details>

<footer>Data: publicreports.mltc.gov.on.ca (guest API). Findings are as issued by the Ministry, not their resolved status.
Verify any figure before relying on it. Generated by ltc.py — open-source tool.</footer>
</div>
<script>
window.HOMES = @@HOMES_JSON@@;
window.YEARS = @@YEARS_JSON@@;
(function(){
  var byK={name:'name',city:'city',findings:'findings',amp:'amp',insp:'insp',co:'co',last:'last'};
  var sortKey='findings', sortDir=-1;
  function val(h,k){ if(k==='findings'){var s=0,lo=+fromV(),hi=+toV();for(var y in h.byyear){var n=+y;if(n>=lo&&n<=hi)s+=h.byyear[y];}return s;} return h[k]||0; }
  function fromV(){var e=document.getElementById('f-from');return e.value||0;}
  function toV(){var e=document.getElementById('f-to');return e.value||9999;}
  function render(){
    var region=document.getElementById('f-region').value, city=document.getElementById('f-city').value, fines=document.getElementById('f-fines').checked;
    var tb=document.querySelector('#tbl tbody'), out=[], shown=0;
    var list=window.HOMES.slice().sort(function(a,b){var x=val(a,sortKey),y=val(b,sortKey);
      if(typeof x==='string'){x=String(x).toLowerCase();y=String(y).toLowerCase();}
      return (x<y?-1:x>y?1:0)*sortDir;});
    list.forEach(function(h){
      if(region&&h.region!==region)return; if(city&&h.city!==city)return; if(fines&&!(h.amp>0))return;
      var f=val(h,'findings'); shown++;
      out.push("<tr><td class='name'><a href='"+h.page+"'>"+h.name+"</a><div class='sub'>"+h.city+"</div></td>"+
        "<td class='num'>"+h.city+"</td><td class='num strong'>"+f+"</td><td class='num'>$"+Math.round(h.amp).toLocaleString()+
        "</td><td class='num'>"+h.insp+"</td><td class='num'>"+h.co+"</td><td class='num'>"+(h.last||"")+"</td></tr>");
    });
    tb.innerHTML=out.join("");
    document.getElementById('f-count').textContent=shown+" of "+window.HOMES.length+" homes";
  }
  document.querySelectorAll('#tbl th').forEach(function(th){
    th.addEventListener('click',function(){var k=th.getAttribute('data-k');
      if(sortKey===k){sortDir*=-1;}else{sortKey=k;sortDir=(k==='name'||k==='city'||k==='last')?1:-1;}render();});
  });
  ['f-region','f-city','f-from','f-to','f-fines'].forEach(function(id){
    document.getElementById(id).addEventListener('change',render);
  });
  document.getElementById('f-to').value=window.YEARS[window.YEARS.length-1]||'';
  render();
})();
</script>
</body></html>
"""
    city_opts = "".join("<option value='%s'>%s</option>" % (esc(c), esc(c)) for c in cities)
    regions = sorted({h["region"] for h in homes_data})
    region_opts = "".join("<option value='%s'>%s</option>" % (esc(r), esc(r)) for r in regions)
    year_opts_all = "".join("<option value='%s'>%s</option>" % (y, y) for y in year_set)
    reps = {
        "@@REGION@@": esc(region_label),
        "@@DATE_RANGE@@": esc(dmin) + " \u2013 " + esc(dmax),
        "@@GENERATED@@": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "@@N_HOMES@@": str(len(by)),
        "@@TOTAL_INS@@": str(total_ins),
        "@@TOTAL_NC@@": str(total_nc),
        "@@TOTAL_CO@@": str(total_co),
        "@@TOTAL_AMP@@": format(int(total_amp), ","),
        "@@REGION_OPTS@@": region_opts,
        "@@CITY_OPTS@@": city_opts,
        "@@YEAR_OPTS@@": year_opts_all,
        "@@ROWS@@": rows_html,
        "@@HOMES_JSON@@": homes_json,
        "@@YEARS_JSON@@": years_json,
    }
    for t, v in reps.items():
        template = template.replace(t, v)
    return template


# ---------------------------------------------------------------- per-home dashboard
def render_home_dashboard(home, insp, findings):
    hn = home["home_number"]
    years = sorted({ (r["date_issued"] or "")[:4] for r in insp if r.get("date_issued") })
    byyear = {}
    for r in insp:
        y = (r["date_issued"] or "")[:4] or "?"
        e = byyear.setdefault(y, dict(wn=0, amp=0.0, insp=0))
        e["wn"] += r["wn_count"] or 0
        e["amp"] += r["amp_total"] or 0
        e["insp"] += 1
    total_wn = sum(r["wn_count"] or 0 for r in insp)
    total_amp = sum(r["amp_total"] or 0 for r in insp)
    maxwn = max((byyear[y]["wn"] for y in years), default=0) or 1

    # trends: findings + inspections + fines by year (CSS bars)
    tcells = []
    for y in years:
        e = byyear[y]
        h = int(100 * e["wn"] / maxwn)
        tcells.append("<div class='col' title='%s: %d findings, %d inspections, $%d'><div class='bar' "
                      "style='height:%dpx'></div><div class='yrval'>%d</div><div class='yrlbl'>%s</div></div>"
                      % (y, e["wn"], e["insp"], int(e["amp"]), max(h, 3), e["wn"], y))
    trends_html = "<div class='barrow tall'>" + "".join(tcells) + "</div>"

    # findings data for the interactive mix + listing
    date_count = Counter(f["date_issued"] for f in findings)
    fdata = []
    listing = []
    cur = None
    for f in findings:  # already date desc
        date = f.get("date_issued") or ""
        fdata.append({"y": date[:4], "c": f["category"], "t": f["type"],
                      "s": f["statute"], "fine": f["fine"] or 0, "date": date,
                      "nc": f["nc"], "src": f["source"], "text": f["text"],
                      "title": f["title"], "num": f["inspection_number"],
                      "url": REPORT_URL.format(iid=f["inspection_id"], hn=hn)})
        if date != cur:
            if cur is not None:
                listing.append("</div>")
            cur = date
            listing.append("<div class='report'><div class='rhead'><a href='%s'>%s</a> · %s · %d finding(s)</div>"
                           % (esc(REPORT_URL.format(iid=f["inspection_id"], hn=hn)), esc(date),
                              esc(f["inspection_number"]), date_count.get(date, 0)))
        fine_badge = ("<span class='badge fine'>$%.0f</span>" % f["fine"]) if f["fine"] else ""
        type_badge = "<span class='badge amp'>FINE</span>" if f["type"] == "AMP" else "<span class='badge wn'>FINDING</span>"
        listing.append(
            "<div class='finding'><div class='fmeta'>%s %s "
            "<span class='statute'>%s</span></div><div class='ftext'>%s</div>"
            "<div class='fsrc'>Sources: %s</div></div>"
            % (type_badge, fine_badge, esc(f["category"]), esc(f["text"] or f["title"]),
               esc(f["source"] or "\u2014")))
    if cur is not None:
        listing.append("</div>")
    listing_html = "".join(listing) if listing else "<p class='note'>No findings recorded.</p>"
    findings_json = json.dumps(fdata, ensure_ascii=False)
    years_json = json.dumps(years)

    template = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='18' fill='%232563eb'/%3E%3Cpath d='M50 30v40M30 50h40' stroke='%23fff' stroke-width='13' stroke-linecap='round'/%3E%3C/svg%3E">
<title>@@NAME@@ — Inspection History</title>
<style>
:root{--ink:#1a1c24;--muted:#6b7280;--line:#e6e8ee;--find:#e0552b;--bg:#f6f7fa;--accent:#2f6df6}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:24px;line-height:1.5}
.wrap{max-width:1000px;margin:0 auto}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.back{font-size:13px;color:var(--muted)}
h1{font-size:22px;margin:6px 0 2px}
.sub{color:var(--muted);font-size:13px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;flex:1 1 130px}
.card .n{font-size:24px;font-weight:700}
.card .l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
section{margin:26px 0}
h2{font-size:16px;margin:0 0 10px}
.barrow{display:flex;align-items:flex-end;gap:10px;height:120px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px}
.barrow.tall{height:150px}
.col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}
.bar{width:70%;max-width:40px;background:var(--find);border-radius:4px 4px 0 0}
.yrval{font-size:11px;font-variant-numeric:tabular-nums;margin-top:3px}
.yrlbl{font-size:11px;color:var(--muted)}
.note{font-size:12px;color:var(--muted);margin-top:8px}
.mixbar{display:flex;align-items:center;gap:10px;margin:6px 0}
.mixlbl{width:280px;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mixtrk{flex:1;background:#eef0f4;border-radius:4px;height:14px}
.mixfill{background:var(--accent);height:14px;border-radius:4px;min-width:2px}
.mixval{width:24px;text-align:right;font-variant-numeric:tabular-nums;font-size:13px}
select{font-size:13px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:#fff}
.report{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 16px;margin-bottom:12px}
.rhead{font-weight:600;margin-bottom:8px}
.finding{border-left:3px solid var(--find);padding:6px 0 6px 12px;margin:8px 0}
.finding .fmeta{font-size:13px;margin-bottom:4px}
.statute{color:var(--muted);font-size:12px}
.ftext{font-size:14px}
.fsrc{font-size:12px;color:var(--muted);margin-top:4px}
.badge{display:inline-block;font-size:10px;font-weight:700;padding:1px 7px;border-radius:10px;margin-right:6px;vertical-align:middle}
.badge.amp{background:#fff0f0;color:var(--find)}
.badge.wn{background:#eef2ff;color:#3a4ea8}
.badge.fine{background:#fdeaea;color:#c0392b}
.fines-toggle{display:inline-flex;align-items:center;gap:6px;margin-left:16px;font-size:13px;color:var(--ink);cursor:pointer;user-select:none}
.fines-toggle input{accent-color:var(--find);width:15px;height:15px;cursor:pointer}
.fines-toggle .fcount{color:var(--muted);font-size:12px}
footer{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
</style></head>
<body><div class="wrap">
<div class="back"><a href="index.html">\u2190 All homes</a></div>
<h1>@@NAME@@</h1>
<div class="sub">@@CITY@@ \u00b7 Licensee: @@LICENSEE@@ \u00b7 <a href="@@URL@@" target="_blank">official report list</a></div>
<div class="cards">
<div class="card"><div class="n">@@INSPECT@@</div><div class="l">Inspections</div></div>
<div class="card"><div class="n">@@FIND@@</div><div class="l">Findings</div></div>
<div class="card"><div class="n">$@@FINES@@</div><div class="l">Fines</div></div>
<div class="card"><div class="n">@@FIRST@@\u2013@@LAST@@</div><div class="l">Reports</div></div>
</div>

<section>
<h2>Findings by year</h2>
@@TRENDS@@
<div class="note">Bar height = written-notification findings issued that year. A tall recent bar is worth checking against the follow-up reports below (a later report notes whether a prior order was remediated).</div>
</section>

<section>
<h2>Finding types</h2>
<label class="note">Year: <select id="mix-year"><option value="">All years</option>@@MIX_YEARS@@</select></label>
<label class="fines-toggle"><input type="checkbox" id="fines-only"> Fines only <span class="fcount" id="fines-count"></span></label>
<div id="mix">@@MIX_INIT@@</div>
</section>

<section>
<h2>All findings</h2>
<div class="note">Grouped by report (newest first). Fines are shown with their amount and rationale. Each report links to the Ministry's source page.</div>
<div id="listing">@@LISTING@@</div>
</section>

<footer>Data: publicreports.mltc.gov.on.ca. Findings are as issued by the Ministry, not their resolved status.
Generated by ltc.py — open-source tool. <a href="index.html">Back to all homes</a>.</footer>
</div>
<script>
window.FINDINGS = @@FINDINGS_JSON@@;
window.YEARS = @@YEARS_JSON@@;
(function(){
  var el=document.getElementById('mix-year');
  var fo=document.getElementById('fines-only');
  var listing=document.getElementById('listing');
  var fcount=document.getElementById('fines-count');
  function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
  function mix(){
    var y=el.value, c={}, tot=0;
    window.FINDINGS.forEach(function(f){ if(f.t!=='WN')return; if(y&&f.y!==y)return; c[f.c]=(c[f.c]||0)+1; tot++; });
    var items=Object.keys(c).map(function(k){return {k:k,v:c[k]};}).sort(function(a,b){return b.v-a.v;});
    var mx=items.length?items[0].v:1;
    if(!items.length){el.parentElement.innerHTML='<div class="note">No findings in this range.</div>';return;}
    var html='<select id="mix-year2" style="display:none"></select>';
    var out='<div style="display:none"></div>';
    items.forEach(function(it){ out+="<div class='mixbar'><div class='mixlbl' title='"+it.k.replace(/'/g,"\\'")+"'>"+it.k+
      "</div><div class='mixtrk'><div class='mixfill' style='width:"+Math.round(100*it.v/mx)+"%'></div></div>"+
      "<div class='mixval'>"+it.v+"</div></div>"; });
    out+="<div class='note'>"+tot+" finding(s) shown. Share of total per category.</div>";
    var box=document.getElementById('mix');
    box.innerHTML=out;
  }
  function renderListing(){
    var y=el.value, only=fo.checked;
    var rows=window.FINDINGS.filter(function(f){ if(only&&f.t!=='AMP')return false; if(y&&f.y!==y)return false; return true; });
    var nAmp=window.FINDINGS.filter(function(f){return f.t==='AMP'&&(!y||f.y===y);}).length;
    fcount.textContent=only?('('+rows.length+' fine'+(rows.length===1?'':'s')+')'):'';
    if(only&&!rows.length){ listing.innerHTML='<p class="note">No fines on record'+(y?' for '+esc(y):'')+'.</p>'; return; }
    var byDate={}, order=[];
    rows.forEach(function(f){ if(!byDate[f.date]){byDate[f.date]=[];order.push(f.date);} byDate[f.date].push(f); });
    order.sort(); order.reverse();
    var out='';
    order.forEach(function(d){
      var g=byDate[d];
      out+="<div class='report'><div class='rhead'><a href='"+esc(g[0].url)+"'>"+esc(d)+"</a> · "+esc(g[0].num)+" · "+g.length+" finding(s)</div>";
      g.forEach(function(f){
        var badge=(f.t==='AMP')?"<span class='badge amp'>FINE</span>":"<span class='badge wn'>FINDING</span>";
        var fine=(f.fine)?"<span class='badge fine'>$"+Math.round(f.fine)+"</span>":"";
        out+="<div class='finding'><div class='fmeta'>"+badge+fine+" <span class='statute'>"+esc(f.c)+"</span></div>"+
             "<div class='ftext'>"+esc(f.text||f.title)+"</div><div class='fsrc'>Sources: "+esc(f.src||"—")+"</div></div>";
      });
      out+="</div>";
    });
    listing.innerHTML=out||'<p class="note">No findings recorded.</p>';
  }
  el.addEventListener('change',function(){mix();renderListing();});
  fo.addEventListener('change',renderListing);
  mix();
  renderListing();
})();
</script>
</body></html>
"""
    reps = {
        "@@NAME@@": esc(home["name"]),
        "@@CITY@@": esc(home.get("city") or ""),
        "@@LICENSEE@@": esc(home.get("licensee") or ""),
        "@@URL@@": esc(HOME_URL.format(hn=hn)),
        "@@INSPECT@@": str(len(insp)),
        "@@FIND@@": str(total_wn),
        "@@FINES@@": format(int(total_amp), ","),
        "@@FIRST@@": esc((insp[0]["date_issued"][:4] if insp else "")),
        "@@LAST@@": esc((insp[-1]["date_issued"][:4] if insp else "")),
        "@@TRENDS@@": trends_html,
        "@@MIX_YEARS@@": "".join("<option value='%s'>%s</option>" % (y, y) for y in reversed(years)),
        "@@MIX_INIT@@": "",
        "@@LISTING@@": listing_html,
        "@@FINDINGS_JSON@@": findings_json,
        "@@YEARS_JSON@@": years_json,
    }
    for t, v in reps.items():
        template = template.replace(t, v)
    return template


# ---------------------------------------------------------------- report
def cmd_report(args):
    c = db()
    homes, insp, findings = load(c)
    if not insp:
        print("No data yet. Run: python3 ltc.py fetch")
        return
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    region_label = args.region

    (out / "index.html").write_text(
        render_dashboard(homes, insp, findings, region_label), encoding="utf-8")

    # per-home dashboards
    homes_by_num = {h["home_number"]: h for h in homes}
    insp_by_home = defaultdict(list)
    for r in insp:
        insp_by_home[r["home_number"]].append(r)
    find_by_home = defaultdict(list)
    for f in findings:
        find_by_home[f["home_number"]].append(f)
    for hn, h in homes_by_num.items():
        hi = sorted(insp_by_home.get(hn, []), key=lambda r: (r["date_issued"] or ""))
        hf = sorted(find_by_home.get(hn, []), key=lambda f: (f.get("date_issued") or ""), reverse=True)
        if not hi:
            continue
        (out / home_file(hn)).write_text(render_home_dashboard(h, hi, hf), encoding="utf-8")

    # CSV: findings (one row per finding)
    with open(str(out / "findings.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["home_number", "home", "date_issued", "inspection_number", "type",
                    "category", "nc", "statute", "fine", "source", "text"])
        for fnd in findings:
            w.writerow([fnd["home_number"], fnd["home_name"], fnd["date_issued"],
                        fnd["inspection_number"], fnd["type"], fnd["category"], fnd["nc"],
                        fnd["statute"], fnd["fine"], fnd["source"], fnd["text"]])

    # CSV: home x year matrix
    by = defaultdict(lambda: dict(wn=0, amp=0.0, insp=0, byyear={}))
    for r in insp:
        b = by[r["home_number"]]
        b["wn"] += r["wn_count"] or 0
        b["amp"] += r["amp_total"] or 0
        b["insp"] += 1
        y = (r["date_issued"] or "")[:4]
        b["byyear"][y] = b["byyear"].get(y, 0) + (r["wn_count"] or 0)
    year_set = sorted({y for b in by.values() for y in b["byyear"]})
    with open(str(out / "home_year.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["home", "city", "licensee"] + year_set + ["total_findings", "total_fines", "inspections"])
        for hn, b in sorted(by.items(), key=lambda kv: -kv[1]["wn"]):
            h = homes_by_num.get(hn, {})
            w.writerow([h.get("name", hn), h.get("city", ""), h.get("licensee", "")] +
                       [b["byyear"].get(y, 0) for y in year_set] +
                       [b["wn"], int(b["amp"]), b["insp"]])

    # console summary
    print("Wrote index.html + %d home pages + findings.csv + home_year.csv in %s"
          % (len(homes_by_num), out))
    print("\n=== HOMES BY TOTAL FINDINGS ===")
    for hn, b in sorted(by.items(), key=lambda kv: -kv[1]["wn"]):
        h = homes_by_num.get(hn, {})
        print("%-40s findings=%-3d fines=$%-7d inspections=%d"
              % ((h.get("name") or hn)[:40], b["wn"], int(b["amp"]), b["insp"]))


def main():
    ap = argparse.ArgumentParser(description="Ontario MLTC LTC inspection fetcher + comparison tool")
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch", help="fetch homes + reports into the cache")
    f.add_argument("--cities", help="comma list of cities (default: Waterloo Region)")
    f.add_argument("--geo", help="text file of search terms, one per line (# comments ok)")
    f.add_argument("--homes", help="comma list of home numbers to fetch only")
    f.add_argument("--force", action="store_true", help="re-fetch even if cached")
    f.add_argument("--delay", type=float, default=DELAY,
                   help="seconds between report-detail calls (default 0.25; use ~0.9 for large province-wide crawls)")
    f.set_defaults(fn=cmd_fetch)
    r = sub.add_parser("report", help="render the HTML site + CSVs from the cache")
    r.add_argument("--out", default=str(BASE / "data" / "report"))
    r.add_argument("--region", default="Ontario", help="label shown on the dashboard")
    r.set_defaults(fn=cmd_report)
    rp = sub.add_parser("reparse", help="re-parse cached raw into findings (no network)")
    rp.set_defaults(fn=cmd_reparse)
    s = sub.add_parser("status", help="show cached data")
    s.set_defaults(fn=cmd_status)
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        ap.print_help()
        sys.exit(1)
    args.fn(args)


if __name__ == "__main__":
    main()
