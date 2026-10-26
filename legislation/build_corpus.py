#!/usr/bin/env python3
"""Rebuild legislation_corpus.json from ontario.ca e-Laws HTML + the project DB.

Self-contained and re-runnable. It:
  1. Fetches every consolidation of O. Reg. 246/22 and FLTCA 2021 (S.O. 2021 c. 39
     sched. 1) from e-Laws (current + historical versions, until 404).
  2. Parses each into section text (corpus.Instrument).
  3. Reads ltc.db for every distinct (instrument, section) cited by findings.
  4. Emits legislation_corpus.json: for each cited section, the canonical text for
     each version period that contains a report date citing it (identical adjacent
     versions merged).

Public-domain legislation (Government of Ontario). Safe to commit to the repo; it is
the offline reference the parser uses to display canonical rule text and to split
regulation from incident narrative.

Usage:  python3 build_corpus.py [path/to/ltc.db]
"""
import re, os, sys, json, glob, datetime, importlib.util, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "_html")          # cache of fetched e-Laws pages
os.makedirs(BASE, exist_ok=True)

spec = importlib.util.spec_from_file_location("corpus", os.path.join(HERE, "corpus.py"))
corpus_mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(corpus_mod)
Instrument = corpus_mod.Instrument
Corpus = corpus_mod.Corpus

def _d(t): return datetime.datetime.strptime(t, "%B %d, %Y").date()
def period_of(path):
    h = open(path).read()
    m = re.search(r"Consolidation Period:\s*From ([A-Z][a-z]+ \d+, \d+)", h)
    if m: return _d(m.group(1)), datetime.date(2099, 1, 1)
    m = re.search(r"Historical version for the period ([A-Z][a-z]+ \d+, \d+) to ([A-Z][a-z]+ \d+, \d+)", h)
    if m: return _d(m.group(1)), _d(m.group(2))
    return None

def fetch(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 5000:
        return dest
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ltc-inspections/1.0 (research)"})
        with urllib.request.urlopen(req, timeout=40) as r, open(dest, "wb") as fh:
            fh.write(r.read())
        return dest if os.path.getsize(dest) > 5000 else None
    except Exception:
        return None

def versions(base_url, name, suffix):
    """Fetch current + historical versions; return [(start, end, path)] sorted."""
    out = []
    cur = fetch(base_url, os.path.join(BASE, name + "_current.html"))
    if cur:
        p = period_of(cur)
        if p: out.append((p[0], p[1], cur))
    i = 1
    while i <= 60:
        f = os.path.join(BASE, f"{name}_v{i}.html")
        pth = fetch(f"{base_url}/v{i}", f)
        if not pth: break
        p = period_of(pth)
        if not p: break
        out.append((p[0], p[1], pth))
        i += 1
    out.sort()
    return out

def build(db_path):
    reg = versions("https://www.ontario.ca/laws/regulation/220246", "reg246", "-e")
    act = versions("https://www.ontario.ca/laws/statute/21f39", "act21f39", "")
    print(f"reg versions: {len(reg)}  act versions: {len(act)}")
    reg_i = {f: Instrument("reg", f, "-e") for _,_,f in reg}
    act_i = {f: Instrument("act", f, "") for _,_,f in act}

    import sqlite3
    c = sqlite3.connect(db_path)
    # distinct (instrument, section) -> set of report dates, using the tolerant parser
    need = {}
    for (statute, datestr) in c.execute("""
        SELECT f.statute, i.date_issued FROM findings f
        JOIN inspections i ON i.inspection_id=f.inspection_id
        WHERE f.statute IS NOT NULL AND f.statute!=''"""):
        inst, sec, _ = Corpus.parse_statute(statute)
        if inst in ("O. Reg. 246/22", "FLTCA, 2021") and sec:
            need.setdefault((inst, sec), set()).add(datestr[:10])

    out = {"O. Reg. 246/22": {}, "FLTCA, 2021": {}}
    for (inst, sec), dates in need.items():
        tl = reg if inst.startswith("O. Reg") else act
        inst_map = reg_i if inst.startswith("O. Reg") else act_i
        files = []
        for d in sorted(dates):
            dd = datetime.date.fromisoformat(d)
            for a, b, f in tl:
                if a <= dd <= b and f not in files:
                    files.append(f)
        variants = []
        for f in files:
            txt = inst_map[f].sections.get(sec, "")
            a = next(x for x in tl if x[2] == f)[0]
            b = next(x for x in tl if x[2] == f)[1]
            if variants and variants[-1]["text"] == txt:
                variants[-1]["end"] = b.isoformat()
            else:
                variants.append({"start": a.isoformat(), "end": b.isoformat(), "text": txt})
        out[inst][sec] = variants
    out["_meta"] = {
        "built": datetime.datetime.now().isoformat(),
        "source": "ontario.ca e-Laws (public domain)",
        "reg_versions": len(reg), "act_versions": len(act),
        "reg_sections": len(out["O. Reg. 246/22"]),
        "act_sections": len(out["FLTCA, 2021"]),
        "note": "Minimal versioned corpus of only the sections cited by findings.",
    }
    dst = os.path.join(HERE, "legislation_corpus.json")
    with open(dst, "w") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    missing = [k for k in need if not out[k[0]].get(k[1], [{}])[0].get("text")]
    print(f"wrote {dst} ({os.path.getsize(dst)} bytes)")
    print(f"cited sections: {len(need)}, missing canonical text: {len(missing)}")
    if missing:
        print("missing:", missing[:10])

if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "ltc.db")
    build(db)
