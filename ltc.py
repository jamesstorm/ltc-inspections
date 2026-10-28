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
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "legislation"))
from corpus import Corpus

API_URL = ("https://publicreports.mltc.gov.on.ca/webruntime/api/apex/execute"
          "?language=en-CA&asGuest=true&htmlEncode=false")
CLASSNAME = "@udd/01pOH000000GbLN"          # Salesforce Apex class id (can change on redeploy)
METHOD_SEARCH = "getHomeDetailsbyNameOrLocarion"
METHOD_REPORT_LIST = "getHomeDetails"
METHOD_REPORT_DETAIL = "getHomeInspectionDetails"
BASE = Path(__file__).resolve().parent
DB = BASE / "ltc.db"
DELAY = 0.25
REGION_CITIES = ["Kitchener", "Waterloo", "Cambridge", "Galt"]
HOME_URL = "https://publicreports.mltc.gov.on.ca/reports?home={hn}&guestUserLang=en-CA"
REPORT_URL = "https://publicreports.mltc.gov.on.ca/reportDetails?detailId={iid}&homeId={hn}&guestUserLang=en-CA"


# ---------------------------------------------------------------- HTTP / API
# Bodies that indicate the upstream Apex class id / method identifiers have changed.
_IDENTIFIER_ERR_MARKERS = ("invalid characters in apex action parameters", "not found")


def _is_identifier_error(e, body):
    """True when a 400 response body signals changed upstream API identifiers."""
    if not isinstance(e, urllib.error.HTTPError) or e.code != 400:
        return False
    low = (body or "").lower()
    return any(m in low for m in _IDENTIFIER_ERR_MARKERS)


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
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if _is_identifier_error(e, body):
                raise RuntimeError(
                    "Upstream API identifiers appear to have changed: Apex class id/method "
                    "'%s' is no longer valid (HTTP %d: %s). Re-discover the current "
                    "classname/method per docs/API.md (DevTools -> Network -> apex/execute "
                    "body) and update CLASSNAME / the METHOD_* constants."
                    % (classname, e.code, (body or str(e)).strip()[:200]))
            last = e
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("api %s failed after %d tries: %s" % (method, retries, last))


def search_homes(term):
    r = api(METHOD_SEARCH, {"homeNameOrLocation": term})
    return r.get("returnValue") or []


def home_report_list(home_number):
    r = api(METHOD_REPORT_LIST, {"accountNumber": str(home_number)})
    rl = (r.get("returnValue") or {}).get("reportList")
    return json.loads(rl) if rl else []


def report_detail(inspection_id, home_number):
    r = api(METHOD_REPORT_DETAIL,
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


FINDING_HEAD = re.compile(
    r"WRITTEN\s+NOTIFICATION:|NOTICE\s+OF\s+ADMINISTRATIVE\s+MONETARY\s+PENALTY\s*\(AMP\)", re.I)
CO_HEAD = re.compile(r"COMPLIANCE\s+ORDER\s+CO\b\s*#?\s*(\d*)\s*", re.I)
# Compliance due date: the phrase is usually embedded at the end of the Sources
# line ("...; This order must be complied with by <date>") rather than a standalone
# line, and the vast majority of reports write "complied with by" — so match it
# anywhere in the text and tolerate the optional "with".
CO_DUE_RE = re.compile(r"This\s+order\s+must\s+be\s+complied\s+(?:with\s+)?by\s+([A-Z][a-z]+\s+\d{1,2},\s*\d{4})")
# WN_Details__c arrives as ministry HTML whose structure varies by report era:
#   2023-era: NC line / "Non-compliance with: <cite>" / violation sentence /
#             "Rationale and Summary" / narrative paragraphs / "Sources:" / [ref]
#   2024-26:  NC line / "Non-compliance with: <cite>" / heading + quoted rule text
#             (recited "s. N (M) ...") / incident narrative / "Sources:"
#   AMP:      header (no title) / failed-to-comply line / "Notice of AMP #N" /
#             "Related to Written Notification NC #N" / s.158 fine paragraph /
#             "Compliance History:" boilerplate
# Parsing works on the HTML line structure, not flattened text: field labels are
# matched line-first (era variants tolerated: "Source:" / "Sources :" / numbered
# "1- " prefixes), quoted rule text is anchored on the "s. N" recitation line,
# and AMP boilerplate is cut at "Compliance History:".

_WN_NEWLINE = re.compile(r"<br\s*/?>|</p>|</li>|</div>|</h\d>|</tr>|</td>|</ul>", re.I)
_WN_ANY_TAG = re.compile(r"<[^>]+>")
_WN_OPEN_TAGGED = re.compile(r"<(?:strong|span|b|i|u|em)\b[^>]*>", re.I)
_WN_CLOSE_TAGGED = re.compile(r"</(?:strong|span|b|i|u|em)>", re.I)
_WN_ZW = re.compile(r"[\u200b\u200c\u200d\ufeff]")


def _wn_lines(wn_html):
    """Split ministry HTML into (text, tagged) lines; empty text = paragraph break.

    Line breaks happen only at true block boundaries (br/p/li/div/h/tr/td/ul);
    inline tags (strong/span/b/i/u/em) emit no text, so a header split
    mid-word (<strong>W</strong><strong>RITTEN NOTIFICATION</strong>) rejoins
    correctly. A double break marks a paragraph. "tagged" = line sat inside a
    styled element (used to tell quoted rule text from plain narrative).
    """
    if not wn_html:
        return []
    in_tagged = 0
    out, buf, buf_tag = [], [], False
    i, n = 0, len(wn_html)
    last_was_break = False

    def flush():
        t = _WN_ZW.sub("", "".join(buf))
        return re.sub(r"\s+", " ", t).strip()

    while i < n:
        if wn_html[i] == "<":
            m = _WN_ANY_TAG.match(wn_html, i)
            if not m:
                buf.append(">"); i += 1; continue
            tag = m.group(0)
            if _WN_NEWLINE.match(tag):
                t = flush()
                if t:
                    out.append((t, buf_tag or in_tagged > 0))
                elif not last_was_break and out:
                    out.append(("", False))
                buf, buf_tag = [], False
                in_tagged = 0
                last_was_break = True
            else:
                last_was_break = False
                if tag.startswith("</"):
                    if _WN_CLOSE_TAGGED.match(tag):
                        in_tagged = max(0, in_tagged - 1)
                elif _WN_OPEN_TAGGED.match(tag):
                    in_tagged += 1
            i = m.end()
        else:
            em = re.match(r"&#?\w+;", wn_html[i:])
            if em:
                buf.append(_h.unescape(em.group(0)))
                if in_tagged: buf_tag = True
                i += len(em.group(0))
            else:
                buf.append(wn_html[i])
                if in_tagged: buf_tag = True
                i += 1
            last_was_break = False
    t = flush()
    if t:
        out.append((t, buf_tag or in_tagged > 0))
    return out


_WN_NC_RE = re.compile(r"^NC\s*#?\s*(\d*)\s+Written\s+Notification\s+pursuant\s+to\s+(.+)$", re.I)
_WN_STAT_LINE_RE = re.compile(r"^Non[-\s]?[Cc]ompliance\s+with\s*[:\s]*(.+)$", re.I)
_WN_RATIONALE_RE = re.compile(r"^(?:\d+[\.)\-]\s*|\d+\s*[-)]\s*)?Rationale\s+and\s+Summary\s*:?\s*(.*)$", re.I)
_WN_SOURCES_RE = re.compile(r"^(?:\d+[\.)\-]\s*|\d+\s*[-)]\s*)?Sources?\s*:\s*(.*)$", re.I)
_WN_RECITE_RE = re.compile(r"^s\.?\s*\d+", re.I)
_WN_CIT_MARK = re.compile(r"\s+\d+\.\s*$")
_WN_AMP_BRIDGE_RE = re.compile(r"^An\s+Administrative\s+Monetary\s+Penalty", re.I)
_WN_AMP_NUM_RE = re.compile(r"^Notice\s+of\s+Administrative\s+Monetary\s+Penalty\s+AMP\s*#?\s*(\d*)", re.I)
_WN_AMP_REL_RE = re.compile(r"Related\s+to\s+Written\s+Notification\s+NC\s*#?\s*(\d*)", re.I)
_WN_AMP_STOP_RE = re.compile(
    r"^(Compliance History|This is the (?:first|second|third|fourth|fifth)"
    r"|Invoice with payment|Licensees must not pay|By submitting a payment)", re.I)
# narrative (incident description) start — used only on untagged lines after a
# recitation (tagged quote lines are exempt). Case-sensitive: narrative lines are
# sentence-case in the source; regulation text opening a line is not.
_WN_NARR_RE = re.compile(
    r"^(?:[A-Z]\)\s"                                        # lettered incident part "A) ..."
    r"|There (?:was|is|were|had)\b"
    r"|The (?:home|resident|DOC|Director|staff)(?:\b|['\u2019])"
    r"|The [Ll]icensee (?!shall|must|will)"
    r"|Staff\b|An incident|A resident"
    r"|(?:\d+|Two|Three|Several|Multiple|Both)\s+(?:staff|residents?|family members?|members?|witnesses?|nurses?|PSWs?)\b"
    r"|\d+\.\s*The [Ll]icensee (?!shall|must|will)"
    r"|In accordance"
    r"|Records\b"
    r"|As per\b"
    r"|A (?:complaint|CIR|Critical Incident Report|critical incident)\b"
    r"|An (?:allegation|incident|accident)\b"
    r"|CIR\b"
    r"|During\b|Ongoing\b|One of\b|It\b"
    r"|On (?:a|the|day|multiple|several|each|numerous)\b"
    r"|When \w+ing\b"
    r"|As part of the"
    r"|\([a-z]{1,3}\)\s+(?:On (?:a|the|several|multiple)|As per|There (?:was|is|were)|Records?\b))")
# unanchored narrative signal: past-tense event language ("was documented",
# "were observed", "was not reported") — regulation text is present/imperative,
# the incident narrative is past-tense. Used in addition to the anchored opener.
_WN_NARR_SUB = re.compile(r"\b(?:was|were)\s+(?:not\s+|had\s+not\s+)?[a-z]+ed\b")
# mid-line narrative: the ministry sometimes concatenates the narrative onto the
# recitation line ("... s. 140 (2).The licensee has failed to ensure ...").
# Split a quote line there — the section boundary + narrative opener must both
# be present, and "shall/must/will/is required" (regulation voice) is excluded.
_WN_NARR_MID = re.compile(
    r"(?:[.)]|^)\s*(?=(?:The [Ll]icensee (?!shall|must|will|is required)"
    r"|Staff\b|An incident|A resident\b|There (?:was|is)\b|Records\b|As per\b"
    r"|In accordance|On (?:a|the|day)\b|When \w+ing\b))")
_WN_MID_SOURCES_RE = re.compile(r"\s+Sources?\s*:\s*")


def _wn_clean_cite(s):
    """Drop the trailing source-citation marker ("... s. 154 (1) 1." -> "... s. 154 (1)")."""
    return _WN_CIT_MARK.sub("", s or "").strip()


def _wn_is_label(t, kind):
    if _WN_NC_RE.match(t) or _WN_RATIONALE_RE.match(t) or _WN_SOURCES_RE.match(t):
        return True
    if kind == "WN":
        return bool(_WN_STAT_LINE_RE.match(t) or _WN_AMP_BRIDGE_RE.match(t))
    if kind == "AMP":
        return bool(_WN_AMP_NUM_RE.match(t) or _WN_AMP_STOP_RE.match(t) or _WN_AMP_BRIDGE_RE.match(t)
                    or re.match(r"^Related\s+to\s+Written\s+Notification", t, re.I))
    return False


def _wn_units(lines, kind):
    """Yield ('label', text, tagged) | ('body', [(text, tagged), ...]) | ('pb', None)."""
    pending = []
    for t, tg in lines[1:]:
        if t == "":
            if pending:
                yield ("body", pending)
                pending = []
            yield ("pb", None)
            continue
        if _wn_is_label(t, kind):
            if pending:
                yield ("body", pending)
                pending = []
            yield ("label", t, tg)
        else:
            pending.append((t, tg))
    if pending:
        yield ("body", pending)


def _wn_split_midsources(lines, res):
    """Split body lines containing a mid-line 'Sources:' label; the tail moves to res['sources']."""
    out = []
    for t, tg in lines:
        m = _WN_MID_SOURCES_RE.search(t)
        if m:
            out.append((t[:m.start()], tg))
            tail = t[m.end():]
            if tail:
                res["sources"] = (res["sources"] + "; " + tail) if res["sources"] else tail
        else:
            out.append((t, tg))
    return [x for x in out if x[0].strip()]


def _wn_split_unit(lines):
    """Split a body unit into (quoted, summary) line lists.

    Quoted rule text = the heading line + the "s. N (M)" recitation + its
    continuations; the incident narrative is the contiguous tail starting at
    the first clearly-narrative line.
    """
    # a narrative can be concatenated onto a recitation line
    # ("... s. 140 (2).The licensee has failed ..."): split it into two lines
    expanded = []
    for t, tg in lines:
        m = _WN_NARR_MID.search(t)
        if m and m.start() > 0:
            expanded.append((t[:m.end()].rstrip(), tg))
            expanded.append((t[m.end():].lstrip(), tg))
        else:
            expanded.append((t, tg))
    lines = expanded
    recite_i = None
    for i, (t, _) in enumerate(lines):
        if _WN_RECITE_RE.match(t):
            recite_i = i
            break
    if recite_i is None:
        return [], [t for t, _ in lines]
    nstart = len(lines)
    for i in range(recite_i + 1, len(lines)):
        t, tg = lines[i]
        # narrative starts at: a tagged->plain transition, OR a narrative-pattern
        # line (2025 reports wrap the narrative in spans too, so the pattern
        # must apply to tagged lines as well)
        if (not tg and lines[i-1][1]) or _WN_NARR_RE.match(t) or _WN_NARR_SUB.search(t):
            nstart = i
            break
    if nstart == len(lines):
        # Fallback (2023-era blocks whose incident prose matches no opener
        # pattern, e.g. "A two-hour repositioning schedule was not immediately
        # initiated..." or "Following a fall incident..."): the regulation
        # quote is the contiguous block from the recitation line — recitation,
        # clause continuations "(a)/1./i.", and imperative/regulation voice.
        # The first plain sentence-case line carrying past-tense incident
        # language, after which no regulation voice remains, starts the
        # narrative. Conservative on purpose: fires only when the primary
        # detectors found nothing.
        def _reg_voice(t):
            return bool(re.search(r"\b(?:shall|must|in accordance with)\b", t, re.I))

        def _clause_start(t):
            return bool(re.match(r"^\s*\(?\s*[a-z0-9]{1,5}\s*[\.\)]\s", t)
                        or re.match(r"^\s*\(\s*[ivxlcdm]+\s*\)\s", t))

        def _past_tense(t):
            return bool(re.search(
                r"\b(?:was|were|failed|did not|received|observed|notified|"
                r"reported|documented|demonstrated|performed|conducted|"
                r"determined|confirmed|witnessed|indicated)\b", t))

        for i in range(recite_i + 1, len(lines)):
            t = lines[i][0]
            if (t[:1].isupper() and _past_tense(t) and not _reg_voice(t)
                    and not _clause_start(t)
                    and not any(_reg_voice(lines[j][0])
                                for j in range(i, len(lines)))):
                nstart = i
                break
    return [t for t, _ in lines[:nstart]], [t for t, _ in lines[nstart:]]


def _wn_parse_block(lines, kind):
    """Parse one finding block (list of (text, tagged) lines, header line first).

    Returns a field dict, or None for a content-less header (doubled
    "WRITTEN NOTIFICATION:" lines in the source data).
    """
    res = {"title": "", "nc": "", "pursuant_to": "", "statute": "", "quoted": "",
           "summary": "", "rationale": "", "sources": "", "related_nc": "", "ampnum": ""}
    h = lines[0][0]
    tm = re.search(r"WRITTEN\s+NOTIFICATION\s*:?\s*(.*)$", h, re.I)
    if tm:
        res["title"] = re.sub(r"^WRITTEN\s+NOTIFICATION\s*:?\s*", "", tm.group(1), flags=re.I).strip()
    mode = None
    stop = False
    body_units = []  # (lines, pre_tag: 'quote' | None)
    amp_title = False
    for ukind, payload, *rest in _wn_units(lines, kind):
        if stop:
            break
        if ukind == "pb":
            continue
        if ukind == "label":
            t = payload
            m = _WN_AMP_BRIDGE_RE.match(t)
            if m and kind == "WN":
                stop = True  # bridge line into the AMP block; stop collecting
                break
            m = _WN_RATIONALE_RE.match(t)
            if m:
                # The ministry's combined "Rationale and Summary" section IS
                # the incident narrative (what happened) — route it into
                # summary, the card's primary text, not a secondary rationale.
                mode = "summary_tail"
                if m.group(1).strip():
                    res["summary"] = (res["summary"] + " " + m.group(1).strip()).strip()
                continue
            m = _WN_SOURCES_RE.match(t)
            if m:
                mode = "sources"
                tail = m.group(1).strip()
                if tail:
                    res["sources"] = (res["sources"] + "; " + tail) if res["sources"] else tail
                continue
            m = _WN_NC_RE.match(t)
            if m:
                if not res["nc"] and not res["pursuant_to"]:
                    res["nc"] = m.group(1)
                    res["pursuant_to"] = _wn_clean_cite(m.group(2))
                continue
            if kind == "WN":
                m = _WN_STAT_LINE_RE.match(t)
                if m:
                    if not res["statute"]:
                        res["statute"] = _wn_clean_cite(m.group(1))
                    else:
                        body_units.append(([(t, True)], "quote"))  # secondary dual-cite context
                    continue
            if kind == "AMP":
                m = _WN_AMP_NUM_RE.match(t)
                if m:
                    res["ampnum"] = m.group(1)
                    continue
                m = _WN_AMP_REL_RE.search(t)
                if m:
                    res["related_nc"] = m.group(1)
                    continue
                if _WN_AMP_STOP_RE.match(t):
                    stop = True  # "Compliance History:" boilerplate follows
                    break
        # body
        ptext_lines = _wn_split_midsources(payload, res)
        if ptext_lines and mode == "sources" and re.match(r"^[A-Z]\)\s", ptext_lines[0][0]):
            mode = None  # a lettered part (A)/B) resumes the incident narrative
        if mode == "summary_tail":
            res["summary"] = (res["summary"] + " " + " ".join(t for t, _ in ptext_lines)).strip()
        elif mode == "sources":
            tail = " ".join(t for t, _ in ptext_lines)
            if tail:
                res["sources"] = (res["sources"] + "; " + tail) if res["sources"] else tail
        elif kind == "WN":
            body_units.append((ptext_lines, None))
        else:  # AMP
            if not amp_title:
                t0 = ptext_lines[0][0]
                m = re.search(r"Notice\s+of\s+Administrative\s+Monetary\s+Penalty\s+AMP\s*#?\s*(\d*)", t0, re.I)
                if m:
                    res["ampnum"] = m.group(1)
                m = _WN_AMP_REL_RE.search(t0)
                if m:
                    res["related_nc"] = m.group(1)
                t0 = re.split(r"Notice\s+of\s+Administrative\s+Monetary\s+Penalty"
                              r"|Related\s+to\s+Written\s+Notification", t0, flags=re.I)[0].strip()
                res["title"] = t0
                amp_title = True
                if len(ptext_lines) > 1:
                    body_units.append((ptext_lines[1:], None))
            else:
                body_units.append((ptext_lines, None))
    # classify WN body units into quoted vs summary
    quoted, summary = [], []
    if kind == "WN":
        parsed = [(_wn_split_unit(u[0]), u[1]) for u in body_units]
        # a short non-narrative summary-only unit right before a quote unit is
        # the rule heading (separated by a paragraph break): fold it into the quote
        for i in range(len(parsed) - 1):
            (q, s), tag = parsed[i]
            (nq, ns), ntag = parsed[i+1]
            if tag is None and not q and s and nq and len(s) == 1 and len(s[0]) < 80 and not _WN_NARR_RE.match(s[0]):
                parsed[i] = ((q, []), tag)
                parsed[i+1] = (([s[0]] + nq, ns), ntag)
        for (q, s), tag in parsed:
            if tag == "quote":
                quoted.extend(q + s)
            else:
                quoted.extend(q)
                summary.extend(s)
    if kind == "WN" and not (res["nc"] or res["statute"] or res["quoted"] or res["summary"]
                            or res["rationale"] or res["sources"]):
        return None
    res["quoted"] = " ".join(quoted).strip()
    # Merge the two narrative sources in document order: the classified
    # untagged summary lines (incident prose before the "Rationale and Summary"
    # section) come first, then the text accumulated under that section.
    _classified = " ".join(summary).strip()
    _accrued = res["summary"].strip()
    res["summary"] = " ".join(x for x in (_classified, _accrued) if x).strip()
    if kind == "AMP" and not res["summary"]:
        res["summary"] = " ".join(t for u in body_units for t, _ in u[0]).strip()
    return res


def parse_findings(wn_html):
    """Split a report's WN_Details HTML into structured finding/fine records.

    Returns one dict per finding:
        seq, type ('WN' | 'AMP'), title, category, nc, statute, fine,
        source, text (plain fallback), pursuant_to, summary, rationale,
        sources, quoted (quoted rule text, 2024+ reports), ampnum
    """
    lines = _wn_lines(wn_html)
    flat = " ".join(t for t, _ in lines if t)
    starts, pos = [], 0  # (flat offset, index into lines)
    for idx, (t, _) in enumerate(lines):
        if t:
            starts.append((pos, idx))
            pos += len(t) + 1
    markers = [(m.start(), "WN" if m.group(0).upper().startswith("WRITTEN") else "AMP")
               for m in FINDING_HEAD.finditer(flat)]

    def line_at(off):
        best = 0
        for off2, idx in starts:
            if off2 <= off: best = idx
            else: break
        return best

    out = []
    for i, (p, kind) in enumerate(markers):
        li = line_at(p)
        le = line_at(markers[i+1][0]) if i+1 < len(markers) else len(lines)
        if le <= li:
            continue  # doubled "WRITTEN NOTIFICATION:" header; second marker owns the block
        r = _wn_parse_block(lines[li:le], kind)
        if r is None:
            continue
        flat_block = " ".join(t for t, _ in lines[li:le])
        finem = re.search(r"administrative penalty of \$\s*([\d,]+(?:\.\d+)?)", flat_block, re.I)
        if not finem:
            finem = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", flat_block)
        fine = float(finem.group(1).replace(",", "")) if finem else 0.0
        nc = r["nc"] or r["related_nc"]
        title = r["title"] or ""
        category = "Administrative Monetary Penalty" if kind == "AMP" else (title or "Written Notification")
        out.append({"seq": len(out)+1, "type": kind, "title": title, "category": category,
                    "nc": nc, "statute": r["statute"], "fine": fine,
                    "source": r["sources"],
                    "text": " ".join(b for b in (r["summary"], r["quoted"], r["rationale"]) if b).strip(),
                    "pursuant_to": r["pursuant_to"], "summary": r["summary"],
                    "rationale": r["rationale"], "sources": r["sources"],
                    "quoted": r["quoted"], "ampnum": r["ampnum"]})
    return out


def parse_co_findings(co_html, co_history=0):
    """Split a report's CO_Details__c / CO_HP_Details__c HTML into CO finding records.

    CO blocks share the WN line structure (heading -> NC line -> Non-compliance
    with -> quoted rule -> narrative -> Sources) plus CO-specific fields
    (ordered actions, due date, Grounds). Returns a list of dicts with keys:
    seq, type ('CO'), category (= title), title, nc, statute, fine (0.0),
    source (alias of sources), text, pursuant_to, summary, rationale (None),
    sources, quoted, ampnum (None), co_number, ordered_actions, due_date,
    co_history.
    """
    lines = _wn_lines(co_html)
    # CO headings are block-start lines ("COMPLIANCE ORDER CO #NNN <title>");
    # body references like "Related to Compliance Order CO #NNN" are mid-line
    # and must not be treated as headings.
    head_idx = [i for i, (t, _) in enumerate(lines) if CO_HEAD.match(t)]
    out = []
    for k, li in enumerate(head_idx):
        le = head_idx[k + 1] if k + 1 < len(head_idx) else len(lines)
        res = _parse_co_block(lines[li:le])
        if res is None:
            continue
        res["seq"] = len(out) + 1
        res["type"] = "CO"
        res["category"] = res["title"] or "Compliance Order"
        res["fine"] = 0.0
        res["ampnum"] = None
        res["rationale"] = None
        res["co_history"] = co_history
        res["source"] = res["sources"]
        res["text"] = " ".join(x for x in (res.get("summary"), res.get("quoted")) if x).strip()
        out.append(res)
    return out


def _parse_co_block(lines):
    """Parse one CO block (list of (text, tagged) lines, header line first)."""
    res = {"co_number": "", "title": "", "nc": "", "pursuant_to": "", "statute": "",
           "quoted": "", "summary": "", "sources": "", "ordered_actions": "",
           "due_date": ""}
    h = lines[0][0]
    hm = CO_HEAD.match(h)
    if hm:
        res["co_number"] = hm.group(1).lstrip("#")
        res["title"] = h[hm.end():].strip()
    mode = None
    body = []
    for t, tg in lines[1:]:
        if t == "":
            continue
        m = _WN_NC_RE.match(t)
        if m:
            if not res["nc"] and not res["pursuant_to"]:
                res["nc"] = m.group(1)
                res["pursuant_to"] = _wn_clean_cite(m.group(2))
            continue
        m = _WN_STAT_LINE_RE.match(t)
        if m:
            if not res["statute"]:
                res["statute"] = _wn_clean_cite(m.group(1))
            continue
        m = _WN_SOURCES_RE.match(t)
        if m:
            mode = "sources"
            tail = m.group(1).strip()
            if tail:
                res["sources"] = (res["sources"] + "; " + tail) if res["sources"] else tail
            continue
        if re.match(r"^The\s+inspector\s+is\s+ordering", t, re.I):
            mode = "order_intro"
            continue
        if re.match(r"^(?:The\s+)?[Ll]icensee\s+shall:", t, re.I):
            mode = "ordered"
            continue
        if re.match(r"^Grounds\s*:?\s*$", t, re.I):
            mode = "grounds"
            continue
        m = CO_DUE_RE.match(t)
        if m:
            res["due_date"] = m.group(1).strip()
            continue
        if mode == "sources":
            tail = t
            if tail:
                res["sources"] = (res["sources"] + "; " + tail) if res["sources"] else tail
        elif mode == "ordered":
            res["ordered_actions"] = (res["ordered_actions"] + " " + t).strip()
        elif mode == "grounds":
            res["summary"] = (res["summary"] + " " + t).strip()
        elif mode == "order_intro":
            # the intro line is boilerplate; ignore
            pass
        else:
            # quoted rule text (recitation + continuations) or narrative
            body.append((t, tg))
    # classify body: quoted rule text (recitation line + continuations) vs narrative
    quoted, summary = [], []
    in_quote = False
    for t, tg in body:
        if _WN_RECITE_RE.match(t):
            in_quote = True
            quoted.append(t)
        elif in_quote and not _WN_NARR_RE.match(t):
            quoted.append(t)
        else:
            in_quote = False
            summary.append(t)
    res["quoted"] = " ".join(quoted).strip()
    if res["summary"]:
        res["summary"] = (res["summary"] + " " + " ".join(summary)).strip()
    else:
        res["summary"] = " ".join(summary).strip()
    # The compliance due date is usually embedded at the end of the Sources line
    # ("...; This order must be complied with by <date>") rather than a standalone
    # line — pull it out of whichever field it landed in.
    if not res["due_date"]:
        for fld in ("sources", "ordered_actions", "summary"):
            m = CO_DUE_RE.search(res[fld] or "")
            if m:
                res["due_date"] = m.group(1).strip()
                res[fld] = (res[fld][: m.start()] + res[fld][m.end():]).strip(" ;")
                break
    if not (res["co_number"] or res["nc"] or res["statute"] or res["summary"] or res["sources"]):
        return None
    return res


def parse_detail(det):
    wn = det.get("WN_Details__c") or ""
    complied = det.get("Complied__c") or ""
    protocols_raw = clean(det.get("Inspection_Protocols__c") or "")
    prot_lines = [l.strip() for l in protocols_raw.splitlines() if l.strip()]
    protocols = [l for l in prot_lines if not re.search(r"Inspection Protocols|were used|protocoles", l, re.I)]
    findings = parse_findings(wn)
    # Compliance Orders: current (CO_Details__c) + prior-year history (CO_HP_Details__c)
    co_findings = parse_co_findings(det.get("CO_Details__c") or "", co_history=0)
    co_findings += parse_co_findings(det.get("CO_HP_Details__c") or "", co_history=1)
    # CO seq continues after the WN/AMP findings within the same report
    base = len(findings)
    for i, f in enumerate(co_findings):
        f["seq"] = base + i + 1
    findings = findings + co_findings
    return {
        "findings": findings,
        "wn_count": sum(1 for f in findings if f["type"] == "WN"),
        "amp_total": sum(f["fine"] for f in findings),
        "co_related": sum(1 for f in co_findings if f["co_history"] == 0),
        "orders_in_compliance": len(re.findall(r"Order\s*#\d+", complied)),
        "intake_type": re.sub(r"\s+", " ", clean(det.get("Intake_Type__c") or "")).strip(),
        "protocols": protocols,
    }


# ---------------------------------------------------------------- canonical enrichment
def _norm_citation(inst, sec, markers):
    """Normalize a parsed citation to 'O. Reg. 246/22, s. 55 (2) (b) (i)' style."""
    s = inst + ", s. " + sec
    for mk in markers:
        if mk.endswith("."):
            s += " " + mk
        else:
            s += " (" + mk + ")"
    return s


def _past_tense_tail(tail):
    """Conservative past-tense incident detector: past-tense language and no
    standalone shall/must imperative."""
    past = r"\b(?:was|were|had|has|have|did)\b|\bfailed to\b"
    if re.search(past, tail, re.I) and not re.search(
            r"\b(?:shall|must)\b", tail, re.I):
        return True
    return False


def apply_canonical(findings, date_issued, corpus):
    """Enrich each finding with canonical statute text + deterministic split.

    Mutates each finding dict in place, setting canonical / canon_mode and
    (conservatively) moving a narrative tail from quoted into summary.
    """
    for f in findings:
        f.setdefault("canonical", "")
        f.setdefault("canon_mode", "")
        statute = f.get("statute") or ""
        if not statute:
            continue
        inst, sec, markers = Corpus.parse_statute(statute)
        if inst not in ("O. Reg. 246/22", "FLTCA, 2021") or sec is None:
            continue
        section_text = corpus.section_text(inst, sec, date_issued)
        if not section_text:
            continue
        if markers:
            prov = Corpus.extract_provision(section_text, markers)
            if len(prov) >= 30 and markers[-1] in prov:
                f["canonical"] = prov
                f["canon_mode"] = "provision"
            else:
                f["canonical"] = section_text
                f["canon_mode"] = "section"
        else:
            f["canonical"] = section_text
            f["canon_mode"] = "section"
        # deterministic split: move narrative tail from quoted into summary.
        # Conservative: the tail must be past-tense incident language (voice
        # check) at any coverage >= 0.5, so regulation-text tails that merely
        # fall outside the canonical n-gram window are never moved.
        quoted = f.get("quoted") or ""
        if f["canonical"] and quoted:
            char_cut, _ = corpus.index(inst, sec, date_issued).split(quoted)
            if char_cut and char_cut > 0 and char_cut < len(quoted):
                coverage = char_cut / len(quoted)
                tail = quoted[char_cut:].strip()
                if coverage >= 0.5 and _past_tense_tail(tail):
                    if len(tail) >= 20 and tail not in (f.get("summary") or ""):
                        f["quoted"] = quoted[:char_cut].rstrip()
                        f["summary"] = ((f.get("summary") or "") + " " + tail).strip()
    return findings


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
        source TEXT, text TEXT, pursuant_to TEXT, summary TEXT, rationale TEXT, sources TEXT);
    CREATE INDEX IF NOT EXISTS ix_findings_home ON findings(home_number);
    CREATE INDEX IF NOT EXISTS ix_findings_insp ON findings(inspection_id);
    """)
    # migrate: add structured columns if missing (idempotent)
    have = {r[1] for r in c.execute("PRAGMA table_info(findings)")}
    for col in ("pursuant_to", "summary", "rationale", "sources", "quoted", "ampnum",
                "co_number", "ordered_actions", "due_date", "canonical", "canon_mode"):
        if col not in have:
            c.execute("ALTER TABLE findings ADD COLUMN %s TEXT DEFAULT ''" % col)
    if "co_history" not in have:
        c.execute("ALTER TABLE findings ADD COLUMN co_history INT")
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
                  "title,nc,statute,fine,source,text,pursuant_to,summary,rationale,sources,"
                  "quoted,ampnum,co_number,ordered_actions,due_date,co_history,canonical,canon_mode) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("%s:%d" % (inspection_id, f["seq"]), inspection_id, str(home_number),
                   f["seq"], f["type"], f["category"], f["title"], f["nc"], f["statute"],
                   f["fine"], f["source"], f["text"], f.get("pursuant_to"), f.get("summary"),
                   f.get("rationale"), f.get("sources"), f.get("quoted"), f.get("ampnum"),
                   f.get("co_number"), f.get("ordered_actions"), f.get("due_date"),
                   f.get("co_history"), f.get("canonical"), f.get("canon_mode")))


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
    corpus = Corpus()  # offline; local JSON in legislation/
    rows = c.execute("SELECT inspection_id, home_number, raw, date_issued FROM inspections WHERE fetched_at IS NOT NULL").fetchall()
    city_map, lic_map = {}, {}
    before = dict(c.execute("SELECT inspection_id, COUNT(*) FROM findings GROUP BY inspection_id").fetchall())
    changed = 0
    total_after = 0
    for iid, hn, raw, date_issued in rows:
        det = json.loads(raw)
        if isinstance(det, str):  # tolerate double-encoded raw
            det = json.loads(det)
        parsed = parse_detail(det)
        apply_canonical(parsed["findings"], date_issued or "", corpus)
        store_detail(c, iid, hn, det, parsed)
        n_after = len(parsed["findings"])
        total_after += n_after
        if before.get(iid, 0) != n_after:
            changed += 1
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
    n_wn = c.execute("SELECT COUNT(*) FROM findings WHERE type='WN'").fetchone()[0]
    n_amp = c.execute("SELECT COUNT(*) FROM findings WHERE type='AMP'").fetchone()[0]
    n_co = c.execute("SELECT COUNT(*) FROM findings WHERE type='CO'").fetchone()[0]
    print("Re-parsed %d reports -> %d findings (%d WN + %d AMP + %d CO)" % (len(rows), n_wn + n_amp + n_co, n_wn, n_amp, n_co))
    print("Reports whose finding count changed: %d" % changed)


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
            "licensee": home_lic.get(hn, ""), "lic": home_lic.get(hn, ""),
            "url": HOME_URL.format(hn=hn),
            "page": home_file(hn), "byyear": b["byyear"], "amp": round(b["amp"], 2),
            "co": b["co"], "insp": b["insp"], "last": b["last"] or "",
        })
    homes_json = json.dumps(homes_data, ensure_ascii=False)
    years_json = json.dumps(year_set)

    # default server-rendered table rows (total findings desc) for no-JS fallback
    rows_html = "".join(
        "<tr data-hn='%s'><td class='name'><a href='%s'>%s</a><div class='sub'>%s</div></td>"
        "<td class='citycell'>%s</td><td class='num strong'>%d</td><td class='num'>$%s</td>"
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
th{background:#f7f8fb;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--ink)}
th.num{text-align:right}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.strong{font-weight:700}
.citycell{white-space:nowrap}
tbody tr:nth-child(even){background:#fafbfd}
tr:hover td{background:#eef2fa}
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
<div class="toolbar">
<label>Search <input type="search" id="f-q" placeholder="Home name…"></label>
<label>Region <select id="f-region"><option value="">All</option>@@REGION_OPTS@@</select></label>
<label>City <select id="f-city"><option value="">All</option>@@CITY_OPTS@@</select></label>
<label>Licensee <select id="f-licensee"><option value="">All</option>@@LICENSEE_OPTS@@</select></label>
<label>Years <select id="f-from">@@YEAR_OPTS@@</select> to <select id="f-to">@@YEAR_OPTS@@</select></label>
<label><input type="checkbox" id="f-fines"> Fines only</label>
<span class="note" id="f-count"></span>
</div>
<div class="cards">
<div class="card"><div class="n" id="agg-homes">@@N_HOMES@@</div><div class="l">Homes</div></div>
<div class="card"><div class="n" id="agg-insp">@@TOTAL_INS@@</div><div class="l">Inspections</div></div>
<div class="card"><div class="n" id="agg-findings">@@TOTAL_NC@@</div><div class="l">Findings</div></div>
<div class="card"><div class="n" id="agg-co">@@TOTAL_CO@@</div><div class="l">CO-related</div></div>
<div class="card"><div class="n" id="agg-fines">$@@TOTAL_AMP@@</div><div class="l">Fines</div></div>
</div>

<section>
<h2>Homes compared by findings</h2>
<table id="tbl">
<thead><tr>
<th data-k="name">Home</th><th data-k="city">City</th>
<th data-k="findings" class="num">Findings</th><th data-k="amp" class="num">Fines</th>
<th data-k="insp" class="num">Inspections</th><th data-k="co" class="num">CO-rel</th>
<th data-k="last" class="num">Last</th>
</tr></thead>
<tbody>@@ROWS@@</tbody>
</table>
<div class="note">Click a column to sort. “Findings” sums written-notification findings across the selected year range. “Fines” = administrative monetary penalties. “CO-rel” = active compliance orders issued in published reports. Click a home name for its dashboard.</div>
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
its type, the regulation section cited, the narrative, the stated source, and any fine. The rule text shown is
canonical legislation text from Ontario e-Laws, versioned to the report date; findings citing pre-2022 instruments
show the ministry's verbatim quote. A home's “Findings”
count is the number of written-notification findings; “Fines” is the sum of AMP amounts; “CO-rel” counts
active compliance orders issued in published reports (prior-year compliance-order history is shown on each
home's page). Counts are by
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
  function selVal(id){var e=document.getElementById(id);return (e&&e.value)||'';}
  function esc2(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
  var FILTER_FIELDS=[{f:'region',sel:'f-region'},{f:'city',sel:'f-city'},{f:'lic',sel:'f-licensee'}];
  function qVal(){var e=document.getElementById('f-q');return ((e&&e.value)||'').trim().toLowerCase();}
  function passes(h, field){
    var region=selVal('f-region'), city=selVal('f-city'), lic=selVal('f-licensee'), q=qVal();
    if(q&&h.name.toLowerCase().indexOf(q)===-1)return false;
    if(field!=='region'&&region&&h.region!==region)return false;
    if(field!=='city'&&city&&h.city!==city)return false;
    if(field!=='lic'&&lic&&h.lic!==lic)return false;
    return true;
  }
  function optionsFor(field){
    var o={};
    window.HOMES.forEach(function(h){if(h[field]&&passes(h,field)){o[h[field]]=1;}});
    return Object.keys(o).sort();
  }
  function syncFilters(){
    FILTER_FIELDS.forEach(function(pair){
      var sel=document.getElementById(pair.sel), cur=sel.value;
      var opts=optionsFor(pair.f), html='<option value="">All</option>';
      opts.forEach(function(v){html+='<option value="'+esc2(v)+'">'+esc2(v)+'</option>';});
      sel.innerHTML=html;
      sel.value=opts.indexOf(cur)!==-1?cur:'';
    });
  }
  function render(){
    var region=document.getElementById('f-region').value, city=document.getElementById('f-city').value, lic=document.getElementById('f-licensee').value, fines=document.getElementById('f-fines').checked, q=qVal();
    var tb=document.querySelector('#tbl tbody'), out=[], shown=0;
    var list=window.HOMES.slice().sort(function(a,b){var x=val(a,sortKey),y=val(b,sortKey);
      if(typeof x==='string'){x=String(x).toLowerCase();y=String(y).toLowerCase();}
      return (x<y?-1:x>y?1:0)*sortDir;});
    list.forEach(function(h){
      if(q&&h.name.toLowerCase().indexOf(q)===-1)return; if(region&&h.region!==region)return; if(city&&h.city!==city)return; if(lic&&h.lic!==lic)return; if(fines&&!(h.amp>0))return;
      var f=val(h,'findings'); shown++;
      out.push("<tr><td class='name'><a href='"+h.page+"'>"+h.name+"</a><div class='sub'>"+h.city+"</div></td>"+
        "<td>"+h.city+"</td><td class='num strong'>"+f+"</td><td class='num'>$"+Math.round(h.amp).toLocaleString()+
        "</td><td class='num'>"+h.insp+"</td><td class='num'>"+h.co+"</td><td class='num'>"+(h.last||"")+"</td></tr>");
    });
    tb.innerHTML=out.join("");
    var sInsp=0, sFind=0, sCo=0, sFine=0;
    list.forEach(function(h){
      if(q&&h.name.toLowerCase().indexOf(q)===-1)return; if(region&&h.region!==region)return; if(city&&h.city!==city)return; if(lic&&h.lic!==lic)return; if(fines&&!(h.amp>0))return;
      sInsp+=h.insp; sFind+=val(h,'findings'); sCo+=h.co; sFine+=h.amp;
    });
    document.getElementById('agg-homes').textContent=shown;
    document.getElementById('agg-insp').textContent=sInsp;
    document.getElementById('agg-findings').textContent=sFind;
    document.getElementById('agg-co').textContent=sCo;
    document.getElementById('agg-fines').textContent='$'+Math.round(sFine).toLocaleString();
    document.getElementById('f-count').textContent=shown+" of "+window.HOMES.length+" homes";
  }
  document.querySelectorAll('#tbl th').forEach(function(th){
    th.addEventListener('click',function(){var k=th.getAttribute('data-k');
      if(sortKey===k){sortDir*=-1;}else{sortKey=k;sortDir=(k==='name'||k==='city'||k==='last')?1:-1;}render();});
  });
  FILTER_FIELDS.forEach(function(pair){
    document.getElementById(pair.sel).addEventListener('change',function(){syncFilters();render();});
  });
  ['f-from','f-to','f-fines'].forEach(function(id){
    document.getElementById(id).addEventListener('change',render);
  });
  document.getElementById('f-q').addEventListener('input',function(){syncFilters();render();});
  document.getElementById('f-to').value=window.YEARS[window.YEARS.length-1]||'';
  syncFilters();
  render();
})();
</script>
</body></html>
"""
    city_opts = "".join("<option value='%s'>%s</option>" % (esc(c), esc(c)) for c in cities)
    regions = sorted({h["region"] for h in homes_data})
    region_opts = "".join("<option value='%s'>%s</option>" % (esc(r), esc(r)) for r in regions)
    licensees = sorted({home_lic.get(hn, "") for hn in by if home_lic.get(hn, "")})
    licensee_opts = "".join("<option value='%s'>%s</option>" % (esc(l), esc(l)) for l in licensees)
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
        "@@LICENSEE_OPTS@@": licensee_opts,
        "@@YEAR_OPTS@@": year_opts_all,
        "@@ROWS@@": rows_html,
        "@@HOMES_JSON@@": homes_json,
        "@@YEARS_JSON@@": years_json,
    }
    for t, v in reps.items():
        template = template.replace(t, v)
    return template


# ---------------------------------------------------------------- per-home dashboard
def _finding_card(f, hn, idx, rel=None):
    """Render one finding as a structured card (server-side, no-JS fallback).

    idx = position in the home's findings array (matches the JS window.FINDINGS
    index) -> stable id fc-<idx> on the card div. rel = optional dict
    {j, nc, topic} of the related WN card for an AMP.
    """
    url = REPORT_URL.format(iid=f["inspection_id"], hn=hn)
    is_amp = f["type"] == "AMP"
    is_co = f["type"] == "CO"
    pills = []
    if is_amp:
        pills.append("<span class='pill pill-fine'>Fine \u2013 AMP</span>")
        if f.get("ampnum"):
            pills.append("<span class='pill pill-nc'>AMP#%s</span>" % esc(f["ampnum"]))
        if f.get("fine"):
            pills.append("<span class='pill pill-fine'>$%s</span>" % format(int(f["fine"]), ","))
    elif is_co:
        pills.append("<span class='pill pill-co'>Compliance Order</span>")
        if f.get("co_number"):
            pills.append("<span class='pill pill-nc'>CO#%s</span>" % esc(f["co_number"]))
        if f.get("co_history"):
            pills.append("<span class='pill pill-nc'>Prior-year</span>")
    else:
        pills.append("<span class='pill pill-wn'>Written notification</span>")
        if f.get("nc"):
            pills.append("<span class='pill pill-nc'>NC#%s</span>" % esc(f["nc"]))
    topic = esc(f.get("title") or f.get("category") or "")
    parts = ["<div class='fcard' id='fc-%d'><div class='cpills'>%s</div>" % (idx, "".join(pills))]
    parts.append("<div class='cref'>%s \u00b7 #%s</div>" % (esc(f["inspection_number"]), esc(f["seq"])))
    if topic:
        parts.append("<div class='ctopic'>%s</div>" % topic)
    if not is_amp and f.get("statute"):
        rule_cite = f["statute"]
        _inst, _sec, _mk = Corpus.parse_statute(rule_cite)
        if _inst in ("O. Reg. 246/22", "FLTCA, 2021") and _sec:
            rule_cite = _norm_citation(_inst, _sec, _mk)
        parts.append("<div class='crule'><span class='crlabel'>Rule cited</span>"
                     "<div class='crtext'>%s</div></div>" % esc(rule_cite))
    rule_text = f.get("canonical") or f.get("quoted") or ""
    if not is_amp and rule_text:
        parts.append("<div class='cquote'><span class='crlabel'>Rule text</span>"
                     "<div class='cquotetext'>%s</div></div>" % esc(rule_text))
        if f.get("canonical"):
            _i2, _s2, _m2 = Corpus.parse_statute(f.get("statute") or "")
            parts.append("<div class='csrc'>Canonical text \u2014 Ontario e-Laws, %s (consolidation in force %s)</div>"
                         % (esc(_i2 or ""), esc(f.get("date_issued") or "")))
    if f.get("summary"):
        parts.append("<div class='csummary'>%s</div>" % esc(f["summary"]))
    if is_co and f.get("due_date"):
        parts.append("<div class='cdue'>Due: %s</div>" % esc(f["due_date"]))
    if is_co and f.get("ordered_actions"):
        items = [a.strip() for a in re.split(r"\s*\d+\)\s*", f["ordered_actions"]) if a.strip()]
        parts.append("<div class='corder'><span class='crlabel'>Ordered actions</span><ol>%s</ol></div>"
                     % "".join("<li>%s</li>" % esc(x) for x in items))
    if rel:  # AMP's related-WN anchor
        parts.append("<div class='crelated'><a class='crel' href='#fc-%d' data-rel='%d'>"
                     "Related to NC#%s \u2014 %s</a></div>"
                     % (rel["j"], rel["j"], esc(rel["nc"]), esc(rel["topic"] or "")))
    if f.get("rationale"):
        parts.append("<div class='crationale'>%s</div>" % esc(f["rationale"]))
    if f.get("sources"):
        parts.append("<div class='csources'>Sources: %s</div>" % esc(f["sources"]))
    parts.append("</div>")
    return "".join(parts)


def _norm_cite_for(f):
    """Normalized citation for a finding, or '' if not in-corpus."""
    st = f.get("statute") or ""
    if not st:
        return ""
    inst, sec, mk = Corpus.parse_statute(st)
    if inst in ("O. Reg. 246/22", "FLTCA, 2021") and sec:
        return _norm_citation(inst, sec, mk)
    return ""


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
    insp_count = Counter(f["inspection_id"] for f in findings)
    # AMP -> related WN relation map (first WN in array order with same num+nc)
    rels = {}
    for i, f in enumerate(findings):
        if f["type"] == "AMP" and f.get("nc"):
            for j, g in enumerate(findings):
                if (g["type"] == "WN" and g["inspection_number"] == f["inspection_number"]
                        and g["nc"] == f["nc"]):
                    rels[i] = {"j": j, "nc": f["nc"], "topic": g["title"] or g["category"] or ""}
                    break
    fdata = []
    listing = []
    cur = None
    for i, f in enumerate(findings):  # already date desc
        date = f.get("date_issued") or ""
        iid = f["inspection_id"]
        fdata.append({"y": date[:4], "c": f["category"], "t": f["type"],
                      "s": f["statute"], "fine": f["fine"] or 0, "date": date,
                      "nc": f["nc"], "src": f["source"], "text": f["text"],
                      "title": f["title"], "num": f["inspection_number"],
                      "pursuant": f.get("pursuant_to"), "summary": f.get("summary"),
                      "rationale": f.get("rationale"), "sources": f.get("sources"),
                      "quoted": f.get("quoted"), "ampnum": f.get("ampnum"),
                      "co_number": f.get("co_number"), "ordered_actions": f.get("ordered_actions"),
                      "due_date": f.get("due_date"), "co_history": f.get("co_history"),
                      "canonical": f.get("canonical"), "canon_mode": f.get("canon_mode"),
                      "norm_cite": _norm_cite_for(f),
                      "seq": f["seq"],
                      "url": REPORT_URL.format(iid=iid, hn=hn)})
        if (date, iid) != cur:  # group by report, not by date
            if cur is not None:
                listing.append("</div>")
            cur = (date, iid)
            listing.append("<div class='report'><div class='rhead'><a href='%s'>%s</a> \u00b7 %s \u00b7 %d finding(s)</div>"
                           % (esc(REPORT_URL.format(iid=iid, hn=hn)), esc(date),
                              esc(f["inspection_number"]), insp_count.get(iid, 0)))
        listing.append(_finding_card(f, hn, i, rels.get(i)))
    if cur is not None:
        listing.append("</div>")
    listing_html = "".join(listing) if listing else "<p class='note'>No findings recorded.</p>"
    findings_json = json.dumps(fdata, ensure_ascii=False)
    years_json = json.dumps(years)

    template = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='18' fill='%232563eb'/%3E%3Cpath d='M50 30v40M30 50h40' stroke='%23fff' stroke-width='13' stroke-linecap='round'/%3E%3C/svg%3E">
<title>@@NAME@@ \u2014 Inspection History</title>
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
/* finding cards */
.fcard{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.cpills{margin-bottom:8px}
.pill{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:12px;margin-right:6px;vertical-align:middle}
.pill-wn{background:#eef3ff;color:#2f6df6}
.pill-nc{background:#eef0f4;color:#6b7280}
.pill-fine{background:#fef3f0;color:#e0552b}
.pill-co{background:#fef9e7;color:#b7950b}
.cdue{font-size:12px;color:#454a55;margin:6px 0;font-weight:600}
.corder{background:#f7f8fb;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:8px 0}
.corder ol{margin:4px 0 0;padding-left:20px}
.corder li{font-size:13px;color:#3d4657;margin:3px 0}
.ctopic{font-size:17px;font-weight:700;margin:2px 0 8px}
.crule{background:#f7f8fb;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:8px 0}
.crlabel{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);display:block;margin-bottom:2px}
.crtext{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;color:var(--ink)}
.cquote{background:#f6f8fb;border:1px solid var(--line);border-left:3px solid #c9d4e8;border-radius:8px;padding:8px 10px;margin:8px 0}
.cquotetext{font-size:13px;color:#3d4657;line-height:1.45;font-style:italic}
.csrc{font-size:11px;color:var(--muted);margin-top:4px}
.csummary{font-size:14px;margin:8px 0}
.crationale{font-size:13px;color:#454a55;margin:6px 0}
.csources{font-size:12px;color:var(--muted);margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}
.cref{font-size:11px;color:var(--muted);margin:2px 0 6px;font-variant-numeric:tabular-nums}
.crelated{margin-top:8px;font-size:13px}
.crel{color:var(--accent);cursor:pointer}
.crel:hover{text-decoration:underline}
.fcard.flash{animation:relflash 1.6s ease-out}
@keyframes relflash{0%{outline:3px solid var(--find);outline-offset:2px}100%{outline:3px solid transparent;outline-offset:2px}}
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
Generated by ltc.py \u2014 open-source tool. <a href="index.html">Back to all homes</a>.</footer>
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
  var RELS={}, revealed={};
  (function(){ window.FINDINGS.forEach(function(f,i){ if(f.t==='AMP'&&f.nc){ for(var j=0;j<window.FINDINGS.length;j++){var g=window.FINDINGS[j]; if(g.t==='WN'&&g.num===f.num&&g.nc===f.nc){RELS[i]={j:j,nc:f.nc,topic:g.title||g.c||''};break;}} } }); })();
  function card(f,i){
    var pills='';
    if(f.t==='AMP'){pills+="<span class='pill pill-fine'>Fine \u2013 AMP</span>"; if(f.ampnum)pills+="<span class='pill pill-nc'>AMP#"+esc(f.ampnum)+"</span>"; if(f.fine)pills+="<span class='pill pill-fine'>$"+Math.round(f.fine).toLocaleString()+"</span>";}
    else if(f.t==='CO'){pills+="<span class='pill pill-co'>Compliance Order</span>"; if(f.co_number)pills+="<span class='pill pill-nc'>CO#"+esc(f.co_number)+"</span>"; if(f.co_history)pills+="<span class='pill pill-nc'>Prior-year</span>";}
    else{pills+="<span class='pill pill-wn'>Written notification</span>"; if(f.nc)pills+="<span class='pill pill-nc'>NC#"+esc(f.nc)+"</span>";}
    var h="<div class='fcard' id='fc-"+i+"'><div class='cpills'>"+pills+"</div>";
    h+="<div class='cref'>"+esc(f.num)+" \u00b7 #"+esc(f.seq)+"</div>";
    if(f.title)h+="<div class='ctopic'>"+esc(f.title)+"</div>";
    if(f.t!=='AMP'&&f.s)h+="<div class='crule'><span class='crlabel'>Rule cited</span><div class='crtext'>"+esc(f.norm_cite||f.s)+"</div></div>";
    if(f.t!=='AMP'&&(f.canonical||f.quoted)){h+="<div class='cquote'><span class='crlabel'>Rule text</span><div class='cquotetext'>"+esc(f.canonical||f.quoted)+"</div></div>"; if(f.canonical)h+="<div class='csrc'>Canonical text \u2014 Ontario e-Laws, "+esc(f.norm_cite.split(',')[0])+" (consolidation in force "+esc(f.date)+")</div>";}
    if(f.summary)h+="<div class='csummary'>"+esc(f.summary)+"</div>";
    if(f.t==='CO'&&f.due_date)h+="<div class='cdue'>Due: "+esc(f.due_date)+"</div>";
    if(f.t==='CO'&&f.ordered_actions){var items=f.ordered_actions.split(/[0-9]+[)]/).filter(function(x){return x.trim();}); h+="<div class='corder'><span class='crlabel'>Ordered actions</span><ol>"+items.map(function(x){return "<li>"+esc(x.trim())+"</li>";}).join("")+"</ol></div>";}
    if(RELS[i]){var r=RELS[i]; h+="<div class='crelated'><a class='crel' href='#fc-"+r.j+"' data-rel='"+r.j+"'>Related to NC#"+esc(r.nc)+" \u2014 "+esc(r.topic)+"</a></div>";}
    if(f.rationale)h+="<div class='crationale'>"+esc(f.rationale)+"</div>";
    if(f.sources)h+="<div class='csources'>Sources: "+esc(f.sources)+"</div>";
    return h+"</div>";
  }
  function flash(id){ var c=document.getElementById(id); if(c){ c.classList.add('flash'); c.scrollIntoView({behavior:'smooth',block:'center'}); setTimeout(function(){ c.classList.remove('flash'); },1600);} }
  function mix(){
    var y=el.value, c={}, tot=0;
    window.FINDINGS.forEach(function(f){ if(f.t!=='WN'&&f.t!=='CO')return; if(y&&f.y!==y)return; c[f.c]=(c[f.c]||0)+1; tot++; });
    var items=Object.keys(c).map(function(k){return {k:k,v:c[k]};}).sort(function(a,b){return b.v-a.v;});
    var mx=items.length?items[0].v:1;
    if(!items.length){el.parentElement.innerHTML='<div class="note">No findings in this range.</div>';return;}
    var out='';
    items.forEach(function(it){ out+="<div class='mixbar'><div class='mixlbl' title='"+it.k.replace(/'/g,"\\'")+"'>"+it.k+
      "</div><div class='mixtrk'><div class='mixfill' style='width:"+Math.round(100*it.v/mx)+"%'></div></div>"+
      "<div class='mixval'>"+it.v+"</div></div>"; });
    out+="<div class='note'>"+tot+" finding(s) shown. Share of total per category.</div>";
    document.getElementById('mix').innerHTML=out;
  }
  function renderListing(){
    var y=el.value, only=fo.checked;
    var rows=window.FINDINGS.filter(function(f,i){ if(revealed[i])return true; if(only&&f.t!=='AMP')return false; if(y&&f.y!==y)return false; return true; });
    var fineCount=window.FINDINGS.filter(function(f){return f.t==='AMP'&&(!y||f.y===y);}).length;
    fcount.textContent=only?('('+fineCount+' fine'+(fineCount===1?'':'s')+')'):'';
    if(only&&!fineCount&&!Object.keys(revealed).length){ listing.innerHTML='<p class="note">No fines on record'+(y?' for '+esc(y):'')+'.</p>'; return; }
    var byKey={}, order=[];
    rows.forEach(function(f){ var k=f.date+'|'+f.num; if(!byKey[k]){byKey[k]=[];order.push(k);} byKey[k].push(f); });
    order.sort(function(a,b){ var da=a.split('|')[0], db=b.split('|')[0]; if(da!==db) return da<db?1:-1; return a<b?-1:1; });
    var out='';
    order.forEach(function(k){
      var g=byKey[k];
      out+="<div class='report'><div class='rhead'><a href='"+esc(g[0].url)+"'>"+esc(g[0].date)+"</a> \u00b7 "+esc(g[0].num)+" \u00b7 "+g.length+" finding(s)</div>";
      g.forEach(function(f){ out+=card(f, window.FINDINGS.indexOf(f)); });
      out+="</div>";
    });
    listing.innerHTML=out||'<p class="note">No findings recorded.</p>';
  }
  listing.addEventListener('click',function(e){
    var a=e.target; while(a&&!(a.classList&&a.classList.contains('crel')))a=a.parentNode;
    if(!a)return; e.preventDefault();
    var j=+a.getAttribute('data-rel');
    if(document.getElementById('fc-'+j)){ flash('fc-'+j); return; }
    revealed[j]=true; renderListing();
    setTimeout(function(){ flash('fc-'+j); }, 60);
  });
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
                    "category", "nc", "statute", "fine", "source", "text",
                    "pursuant_to", "summary", "rationale", "sources", "quoted", "ampnum",
                    "co_number", "ordered_actions", "due_date", "co_history",
                    "canonical", "canon_mode"])
        for fnd in findings:
            w.writerow([fnd["home_number"], fnd["home_name"], fnd["date_issued"],
                        fnd["inspection_number"], fnd["type"], fnd["category"], fnd["nc"],
                        fnd["statute"], fnd["fine"], fnd["source"], fnd["text"],
                        fnd.get("pursuant_to"), fnd.get("summary"), fnd.get("rationale"),
                        fnd.get("sources"), fnd.get("quoted"), fnd.get("ampnum"),
                        fnd.get("co_number"), fnd.get("ordered_actions"), fnd.get("due_date"),
                        fnd.get("co_history"), fnd.get("canonical"), fnd.get("canon_mode")])

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