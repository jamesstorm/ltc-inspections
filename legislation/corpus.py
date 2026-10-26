"""Per-section clause corpus from ontario.ca e-Laws HTML, + n-gram quote splitter.

Handles the full set of structural classes (both the `-e` regulation suffix and the
no-suffix Act convention) and bilingual pages (first occurrence of each section wins).
"""
import re, html as _h, json

def _norm(s):
    s = _h.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"(O\.\s*Reg|FLTCA|S\.O|C\.C\.S)\.?\s*[\d/,\s]*s\.\s*\d+(?:\s*\([\d.]+\))*\.?", " ", s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

_TOQS = ["section","subsection","subsubsection","paragraph","parawtab","clause",
         "subclause","subpara","subsubclause","subsubpara","equation","definition",
         "Sdefinition","defclause","defsubclause","comment","Pnote","firstdef"]
_YTOQS = ["section","subsection","paragraph","subpara","headnote","definition"]

def _patterns(suffix):
    if suffix:
        toks = [t + suffix for t in _TOQS]
    else:
        # Act files: regular classes plus "Y" classes (not-yet-in-force sections)
        toks = _TOQS + ["Y" + t for t in _YTOQS]
    pat = re.compile(r'<p class="(?:%s)">(.*?)</p>' % "|".join(toks), re.S)
    sec = re.compile(r'<p class="(?:section%s|Ysection%s)">(.*?)</p>' % (suffix, suffix), re.S)
    return pat, sec

_SECNUM = re.compile(r"<a name=\"[^\"]*\"></a>\s*<strong>(?:<span>)?(\d+(?:\.\d*)?)[ \t]*(?:</span>)?\s*</strong>")

class Instrument:
    def __init__(self, name, path, suffix="-e"):
        self.name = name
        h = open(path).read()
        pat, sec = _patterns(suffix)
        sec_starts = []
        for m in sec.finditer(h):
            nm = _SECNUM.search(m.group(1))
            if nm:
                sec_starts.append((nm.group(1).rstrip("."), m.start(), m.end()))
        self.sections = {}
        for i, (num, s, e) in enumerate(sec_starts):
            if num in self.sections:
                continue
            end = sec_starts[i+1][1] if i+1 < len(sec_starts) else len(h)
            head = _norm(_SECNUM.sub("", sec.search(h[s:e]).group(1)))
            span = h[e:end]
            buf = [head] if head else []
            for m2 in pat.finditer(span):
                t = _norm(m2.group(1))
                if t:
                    buf.append(t)
            self.sections[num] = " ".join(buf)

    def section_text(self, N):
        return self.sections.get(str(N).rstrip("."), "")

def _strip_cites(s):
    """Remove embedded e-Laws-style citations (present in ministry quotes too)."""
    s = re.sub(r"(O\.\s*Reg|FLTCA|S\.O|C\.C\.S)\.?\s*[\d/,\s]*s\.\s*\d+(?:\s*\([\d.\)]*\))*\.?", " ", s)
    s = re.sub(r"\bO\.\s*Reg\.\s*\d+/\d+\b", " ", s)
    return s

def _words(s):
    s = _strip_cites(s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9']+", " ", s)
    return [w for w in s.split() if w]

_CITE_SPAN = re.compile(r"(O\.\s*Reg|FLTCA|S\.O|C\.C\.S)\.?\s*[\d/,\s]*s\.\s*\d+(?:\s*\([\d.\)]*\))*\.?|\bO\.\s*Reg\.\s*\d+/\d+\b")

class NgramIndex:
    def __init__(self, text, n=8):
        self.n = n
        self.words = _words(text)
        self.ngrams = set(tuple(self.words[i:i+n]) for i in range(len(self.words)-n+1))

    def split(self, quote):
        """Return (char_cut, quote). char_cut = character offset in `quote` where the
        narrative begins (everything before is regulation text).

        Citations are stripped for matching (they appear in both the canonical text and
        the ministry quote) but the returned cut maps back to the ORIGINAL quote so the
        regulation / narrative halves keep their exact wording.
        """
        n = self.n
        ow = [(m.group(0), m.start()) for m in re.finditer(r"\S+", quote)]
        if not ow:
            return 0, quote
        cite_spans = [m.span() for m in _CITE_SPAN.finditer(quote)]
        stripped = []   # (stripped_word, orig_index)
        for idx, (w, st) in enumerate(ow):
            if any(cs <= st < ce for cs, ce in cite_spans):
                continue
            sw = _words(w)
            if sw:
                stripped.append((sw[0], idx))
        sw = [w for w, _ in stripped]
        if len(sw) < n:
            return 0, quote
        matched = [False]*len(sw)
        for i in range(len(sw) - n + 1):
            if tuple(sw[i:i+n]) in self.ngrams:
                for j in range(i, i+n):
                    matched[j] = True
        last = -1
        for i, ok in enumerate(matched):
            if ok:
                last = i
        if last < 0:
            return 0, quote
        if last + 1 < len(stripped):
            orig_idx = stripped[last + 1][1]
            char_cut = ow[orig_idx][1]
        else:
            char_cut = len(quote)
        return char_cut, quote

import os as _os, datetime as _dt

def _parse_date(s):
    return _dt.datetime.strptime(s, "%Y-%m-%d").date()

class Corpus:
    """Production loader for legislation_corpus.json.

    - section_text(instrument, section, date) -> canonical section text for the
      consolidation in effect on `date` (ISO string or date).
    - index(instrument, section, date) -> NgramIndex over that text (for splitting).
    - parse_statute(statute) -> (instrument, section, [provision markers]) or (None,..).
    - extract_provision(section_text, markers) -> the exact cited provision text.
    """
    def __init__(self, path=None):
        if path is None:
            path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                 "legislation_corpus.json")
        with open(path) as fh:
            self._data = json.load(fh)
        self._reg = self._data.get("O. Reg. 246/22", {})
        self._act = self._data.get("FLTCA, 2021", {})

    def _pick(self, variants, date):
        if not variants:
            return ""
        d = _parse_date(date) if isinstance(date, str) else date
        chosen = None
        for v in variants:
            a = _parse_date(v["start"]); b = _parse_date(v["end"])
            if a <= d <= b:
                chosen = v
                break
        if chosen is None:
            # nearest variant whose start is <= d (e.g. date just before first period)
            for v in variants:
                if _parse_date(v["start"]) <= d and (
                        chosen is None or _parse_date(v["start"]) > _parse_date(chosen["start"])):
                    chosen = v
        return chosen.get("text", "") if chosen else ""

    def section_text(self, instrument, section, date):
        src = self._reg if instrument.startswith("O. Reg") else self._act
        return self._pick(src.get(str(section), []), date)

    def index(self, instrument, section, date):
        return NgramIndex(self.section_text(instrument, section, date))

    @staticmethod
    def parse_statute(statute):
        """Tolerant statute parser. Handles the free-text variants in the data
        (missing periods, '0. Reg' for 'O. Reg', 'FLCTA', '246/ 22', old instruments).
        Returns (instrument, section, [provision markers]); instrument in {O. Reg. 246/22,
        O. Reg. 79/10, LTCHA, 2007, FLTCA, 2021} — only the first and last are in the corpus."""
        st = (statute or "").strip()
        stn = st.replace("0. Reg", "O. Reg").replace("0 . Reg", "O. Reg")
        stn = stn.replace("FLCTA", "FLTCA").replace("Fixing Long-Term Care Act", "FLTCA")
        stn = re.sub(r"\bO\.\s*Reg\s*\.?\b", "O. Reg.", stn)      # O. Reg / O.Reg / O. Reg.
        stn = stn.replace("246/ 22", "246/22").replace("79/ 10", "79/10")
        if "246/22" in stn:
            inst = "O. Reg. 246/22"
        elif "79/10" in stn:
            inst = "O. Reg. 79/10"
        elif "LTCHA" in stn or "Long-Term Care Homes Act" in stn:
            inst = "LTCHA, 2007"
        elif "FLTCA" in stn or "S.O. 2021" in stn or "S.o. 2021" in stn:
            inst = "FLTCA, 2021"
        else:
            return None, None, []
        m = re.search(r"\bs(?:ection)?\.?\s*(\d+(?:\.\d+)?)", stn)
        if not m:
            return inst, None, []
        sec = m.group(1)
        rest = stn[m.end():]
        rest = re.split(r"\b(?:of|under|the|and|in accordance)\b", rest, flags=re.I)[0]
        return inst, sec, Corpus._prov_markers(rest)

    @staticmethod
    def _prov_markers(rest):
        """Parse trailing '(2) (b) (i)' / '19. iv.' / '(7)' into ordered markers."""
        markers = []
        for m in re.finditer(r"\((\d+(?:\.\d+)?)\)|\(([a-z]{1,3})\)|\b(\d+)\.\s|([ivxlcdm]{1,6})\.", rest):
            if m.group(1) is not None:
                markers.append(m.group(1))
            elif m.group(2) is not None:
                markers.append(m.group(2))
            elif m.group(3) is not None:
                markers.append(m.group(3) + ".")
            elif m.group(4) is not None:
                markers.append(m.group(4) + ".")
        return markers

    @staticmethod
    def extract_provision(section_text, markers):
        """Return the text of the cited provision: from the last cited marker to the
        next sibling-or-higher-level marker (or end). markers is a list from
        _prov_markers, e.g. ['2','b','i'] or ['19.'] or ['7']."""
        if not section_text:
            return ""
        if not markers:
            return section_text.strip()
        # Walk markers; find each in the running segment, then keep the text from the
        # final marker to the next marker of same/higher rank.
        seg = section_text
        for k, mk in enumerate(markers):
            last = (k == len(markers) - 1)
            pat = re.escape(mk)
            # marker appears as "(mk)", "mk.", or "mk "
            m = re.search(r"\(?\s*" + pat + r"(?:\)\s*|\.\s*|\s+)", seg)
            if not m:
                return seg.strip()  # can't locate; fall back to current segment
            if last:
                # from here to the next sibling-or-higher marker
                tail = seg[m.end():]
                rank = _marker_rank(mk)
                nxt = _next_sibling(tail, rank)
                return (seg[m.start():m.end()] + (tail[:nxt] if nxt is not None else tail)).strip()
            seg = seg[m.start():]
        return seg.strip()

def _marker_rank(mk):
    # numeric subsection = rank 1, letter clause = rank 2, roman/numbered sub = rank 3
    if re.fullmatch(r"\d+(\.\d+)?", mk):
        return 1
    if re.fullmatch(r"[a-z]{1,3}", mk):
        return 2
    return 3

def _next_sibling(text, rank):
    """index of the next marker of rank <= `rank` (a sibling or parent), else None."""
    best = None
    for m in re.finditer(r"\(?\s*(\d+(?:\.\d+)?|[a-z]{1,3})\s*\)\s|(\d+)\.\s", text):
        mk = m.group(1) or (m.group(2) + ".")
        r = _marker_rank(mk)
        if r <= rank:
            best = m.start()
            break
    return best
