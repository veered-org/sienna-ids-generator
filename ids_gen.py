#!/usr/bin/env python3
"""ids-gen -- build an Information Disclosure Statement from a list of references.

Give it patent numbers, publication numbers or a folder of prior-art PDFs and it
looks up the filing date, the issue or publication date and the first named
inventor for each one, sorts them into the three tables the USPTO expects
(US patents, US published applications, foreign documents and other literature)
and writes a Word document you can finish and file, plus a CSV of what it found.

Nothing is filed for you, and nothing is legal advice: check every line against
the reference itself before it goes to the Patent Office.

    ids-gen --out IDS.docx 10000000 US9876543B2 2015/0123456 EP1234567A1
    ids-gen --from-folder "Prior Art" --applicant "Smith" --app-no 19/801,543
"""

import argparse, csv, datetime, os, re, sys, time, urllib.error, urllib.parse, urllib.request

UA = "Mozilla/5.0 (ids-gen; +https://veered.org/ids-generator/)"
GP = "https://patents.google.com/patent/%s/en"
ODP = "https://api.uspto.gov/api/v1/patent/applications/search"

# --------------------------------------------------------------------------- #
#  Reading the numbers                                                         #
# --------------------------------------------------------------------------- #

US_PUB = re.compile(r"^(?:US)?\s*(20\d{2})[/\-\s]?(\d{6,7})\s*([A-Z]\d?)?$", re.I)
US_PAT = re.compile(r"^(?:US)?\s*([0-9]{1,2},?[0-9]{3},?[0-9]{3})\s*([A-Z]\d?)?$", re.I)
FOREIGN = re.compile(r"^([A-Z]{2})\s*([0-9][0-9 ,/\-]*)\s*([A-Z]\d?)?$", re.I)

# The T column (a checked box = an English-language translation is provided), as on
# the SB-1449 and, from November 5, 2026, the PTO-892. It starts checked for a foreign
# document unless the office publishes in English, or may have (EP and WO: read the
# reference, then check the box yourself).
ENGLISH_OFFICES = {"AU", "CA", "GB", "IE", "IN", "NZ", "SG", "ZA"}
EITHER_LANGUAGE = {"EP", "WO"}


class Ref:
    """One reference, in the shape the IDS tables want."""

    def __init__(self, raw):
        self.raw = raw.strip()
        self.kind = ""          # A1, B2 ...
        self.country = "US"
        self.number = ""
        self.sort_key = ""
        self.table = "npl"      # us_patent | us_pub | foreign | npl
        self.filed = ""
        self.dated = ""
        self.inventor = ""
        self.title = ""
        self.note = ""
        self._classify()

    def _classify(self):
        s = re.sub(r"\s+", " ", self.raw).strip()
        m = US_PUB.match(s)
        if m:
            self.table, self.number = "us_pub", "%s/%s" % (m.group(1), m.group(2).zfill(7))
            self.kind = (m.group(3) or "A1").upper()
            self.sort_key = self.number
            return
        m = US_PAT.match(s)
        if m:
            self.table = "us_patent"
            self.number = m.group(1).replace(",", "")
            self.kind = (m.group(2) or "").upper()
            self.sort_key = self.number.zfill(9)
            return
        m = FOREIGN.match(s)
        if m and m.group(1).upper() != "US":
            self.table = "foreign"
            self.country = m.group(1).upper()
            self.number = re.sub(r"[ ,]", "", m.group(2))
            self.kind = (m.group(3) or "").upper()
            self.sort_key = self.country + self.number
            return
        self.table, self.number, self.sort_key = "npl", s, s.lower()

    # ---- the id Google Patents uses ------------------------------------- #
    def google_id(self):
        if self.table == "us_patent":
            return "US%s%s" % (self.number, self.kind or "")
        if self.table == "us_pub":
            return "US%s%s" % (self.number.replace("/", ""), self.kind or "A1")
        if self.table == "foreign":
            return "%s%s%s" % (self.country, self.number, self.kind or "")
        return None

    def display_number(self):
        if self.table == "us_patent":
            n = self.number
            return "{:,}".format(int(n)) if n.isdigit() else n
        if self.table == "us_pub":
            return "US " + self.number
        if self.table == "foreign":
            return "%s %s" % (self.country, self.number)
        return self.number

    def translation_checked(self):
        return (self.table == "foreign"
                and self.country not in ENGLISH_OFFICES | EITHER_LANGUAGE)


def refs_from_folder(folder):
    """Every PDF in a folder, read as 'the file name is the number'."""
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".pdf"):
            continue
        stem = os.path.splitext(name)[0]
        stem = re.sub(r"[_\-]+", " ", stem)
        m = re.match(r"\W*((?!US)[A-Z]{2}\s*\d[\d ,/]{4,}\d(?:\s*[A-Z]\d?(?![A-Za-z]))?)", stem, re.I)   # a foreign number, whatever its length
        if m:
            out.append(Ref(m.group(1)))
            continue
        m = re.search(r"((?:US|EP|WO|GB|DE|FR|JP|CN|KR|CA|IL)?\s*"
                      r"(?:20\d{2}[/\-\s]?\d{6,7}|[0-9]{1,2},?[0-9]{3},?[0-9]{3})"
                      r"(?:\s*[A-Z]\d?(?![A-Za-z]))?)", stem, re.I)     # a kind code is a lone letter, not the "s" of "see"
        out.append(Ref(m.group(1) if m else stem))
    return out


# --------------------------------------------------------------------------- #
#  Looking each one up                                                         #
# --------------------------------------------------------------------------- #

def fetch(ref, pause=0.6):
    """Fill in the dates and the inventor from Google Patents' own metadata."""
    gid = ref.google_id()
    if not gid:
        ref.note = "not looked up (not a patent number)"
        return ref
    url = GP % urllib.parse.quote(gid)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    except Exception as e:                                  # offline, 404, blocked
        ref.note = "lookup failed (%s)" % e.__class__.__name__
        return ref
    time.sleep(pause)                                       # be a polite visitor
    def meta(name, scheme=None):
        pat = r'<meta name="%s" content="([^"]*)"%s' % (
            name, r'[^>]*scheme="%s"' % scheme if scheme else "")
        m = re.search(pat, html)
        return m.group(1) if m else ""
    def bare_date():
        """A publication has a plain DC.date with no scheme: that is its
        publication date. A granted patent's is tagged scheme="issue"."""
        for m in re.finditer(r'<meta name="DC\.date" content="([^"]*)"([^>]*)>', html):
            if "scheme=" not in m.group(2):
                return m.group(1)
        return ""
    ref.filed = meta("DC.date", "dateSubmitted")
    ref.dated = meta("DC.date", "issue") or bare_date()
    ref.inventor = meta("DC.contributor", "inventor")
    ref.title = re.sub(r"\s+", " ", meta("DC.title")).strip()
    if not (ref.filed or ref.dated):
        ref.note = "nothing found -- check the number"
    return ref


def fetch_odp(ref, key, pause=0.25):
    """Fill in the dates and the inventor from the USPTO's own Open Data Portal.

    Google Patents refuses many servers (data centres, shared hosts), and the
    USPTO's data is the primary record anyway. Needs a free API key from
    https://data.uspto.gov/apis/getting-started (pass --odp-key or set
    USPTO_ODP_KEY). Covers US patents and US publications; foreign documents
    and literature are left for you to complete by hand."""
    import json
    if ref.table == "us_patent":
        q = "applicationMetaData.patentNumber:%s" % ref.number
    elif ref.table == "us_pub":
        q = "applicationMetaData.earliestPublicationNumber:*%s*" % ref.number.replace("/", "")
    else:
        ref.note = ("not looked up (USPTO data covers US documents only)"
                    if ref.table == "foreign" else "not looked up (not a patent number)")
        return ref
    url = ODP + "?" + urllib.parse.urlencode(
        {"q": q, "fields": "applicationMetaData", "limit": 1})
    body, code = "", 0
    for attempt in range(4):                                # the portal throttles bursts: back off on 429
        try:
            req = urllib.request.Request(url, headers={"X-API-KEY": key, "User-Agent": UA,
                                                       "Accept": "application/json"})
            body = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
            code = 200
        except urllib.error.HTTPError as e:
            body, code = "", e.code
        except Exception as e:                              # offline, timeout
            ref.note = "lookup failed (%s)" % e.__class__.__name__
            return ref
        if code not in (429, 502, 503):
            break
        time.sleep(1.5 * (attempt + 1))
    time.sleep(pause)
    if code == 404:
        ref.note = "nothing found -- check the number"
        return ref
    if code != 200:
        ref.note = "lookup failed (HTTP %s)" % code
        return ref
    try:
        bag = json.loads(body).get("patentFileWrapperDataBag") or []
        m = bag[0]["applicationMetaData"]
    except Exception:
        ref.note = "nothing found -- check the number"
        return ref
    ref.filed = m.get("filingDate", "") or ""
    if ref.table == "us_patent":
        ref.dated = m.get("grantDate", "") or ""
    else:
        ref.dated = m.get("earliestPublicationDate", "") or ""
    ref.inventor = m.get("firstInventorName", "") or ""
    ref.title = re.sub(r"\s+", " ", m.get("inventionTitle", "") or "").strip()
    if not (ref.filed or ref.dated):
        ref.note = "nothing found -- check the number"
    return ref


def fetch_local(ref, db_path):
    """Fill in a US patent from the offline index built by build_old_db.py.

    The Open Data Portal starts with applications filed in 2001, so a 1990s
    patent comes back "not found" there. The index holds every grant from 1976
    that the portal lacks (number, filing date, issue date, first inventor,
    title), read from PatentsView's bulk data."""
    import sqlite3
    if ref.table != "us_patent":
        return ref
    try:
        con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(db_path), uri=True)
        row = con.execute("SELECT filed, dated, inventor, title FROM patents WHERE number = ?",
                          (ref.number,)).fetchone()
        con.close()
    except sqlite3.Error as e:
        ref.note = "lookup failed (local index: %s)" % e
        return ref
    if not row:
        return ref
    ref.filed, ref.dated, ref.inventor, ref.title = [x or "" for x in row]
    ref.note = "" if (ref.filed or ref.dated) else ref.note
    return ref


def lookup(ref, source, odp_key="", old_db="", google=True):
    """Try the sources in turn until one has the reference.

    odp:    the portal, then the local index (for pre-2001 patents), then
            Google Patents unless google=False.
    google: Google Patents, then the local index."""
    order = ["odp", "local", "google"] if source == "odp" else ["google", "local"]
    for src in order:
        if src == "local" and not old_db:
            continue
        if src == "google" and not google:
            continue
        if src == "odp":
            fetch_odp(ref, odp_key)
        elif src == "local":
            fetch_local(ref, old_db)
        else:
            note = ref.note
            fetch(ref)
            if ref.note and note:            # keep the more telling of two failures
                ref.note = note if note.startswith("nothing found") else ref.note
        if ref.filed or ref.dated or ref.note.startswith("not looked up"):
            return ref
    return ref


# --------------------------------------------------------------------------- #
#  Writing the document                                                        #
# --------------------------------------------------------------------------- #

HEAD = [("us_patent", "U.S. PATENTS",
         ["Cite No.", "Patent Number", "Kind Code", "Issue Date", "Name of Patentee"]),
        ("us_pub", "U.S. PATENT APPLICATION PUBLICATIONS",
         ["Cite No.", "Publication Number", "Kind Code", "Publication Date", "Name of Applicant"]),
        ("foreign", "FOREIGN PATENT DOCUMENTS",
         ["Cite No.", "Country", "Number", "Kind Code", "Date", "Name of Applicant", "T"]),
        ("npl", "NON-PATENT LITERATURE DOCUMENTS",
         ["Cite No.", "Document"])]

# Column widths in inches (6.5" of text width). "Cite No." and "Kind Code" are just wide
# enough for their headings; the number and name columns take the rest.
WIDTHS = {"us_patent": [0.8, 1.9, 0.95, 1.0, 1.85],
          "us_pub":    [0.8, 1.9, 0.95, 1.0, 1.85],
          "foreign":   [0.8, 0.8, 1.25, 0.95, 1.0, 1.35, 0.35],
          "npl":       [0.8, 5.7]}


def _set_widths(table, widths):
    from docx.shared import Inches
    table.autofit = False
    for col, w in zip(table.columns, widths):      # the grid (what LibreOffice reads)
        col.width = Inches(w)
    for row in table.rows:                         # the cells (what Word reads)
        for cell, w in zip(row.cells, widths):
            cell.width = Inches(w)


def signature_date(today=None):
    """Today's date as it goes under the signature: October 19, 2026."""
    d = today or datetime.date.today()
    return "%s %d, %d" % (d.strftime("%B"), d.day, d.year)


def _underline_cell(cell):
    """A bottom border on one table cell: the line the signature goes on."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    tcPr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    bottom = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "0"), ("w:color", "000000")):
        bottom.set(qn(k), v)
    borders.append(bottom)
    tcPr.append(borders)


def _checkbox(paragraph, checked):
    """A Word checkbox control: one click checks or clears it."""
    from docx.oxml import parse_xml
    glyph, val = ("\u2612", "1") if checked else ("\u2610", "0")
    paragraph._p.append(parse_xml(
        '<w:sdt xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:sdtPr>'
        '<w:alias w:val="English-language translation provided"/>'
        '<w14:checkbox><w14:checked w14:val="%s"/>'
        '<w14:checkedState w14:val="2612" w14:font="Segoe UI Symbol"/>'
        '<w14:uncheckedState w14:val="2610" w14:font="Segoe UI Symbol"/></w14:checkbox>'
        '</w:sdtPr><w:sdtContent><w:r><w:rPr>'
        '<w:rFonts w:ascii="Segoe UI Symbol" w:hAnsi="Segoe UI Symbol" w:eastAsia="Segoe UI Symbol" '
        'w:cs="Segoe UI Symbol"/></w:rPr><w:t>%s</w:t></w:r></w:sdtContent></w:sdt>' % (val, glyph)))


def write_docx(refs, path, applicant="", app_no="", filed="", title="",
               examiner="N/A", art_unit="N/A", attorney="", docket="", reg_no="",
               today=None):
    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    for s in doc.sections:
        s.page_width, s.page_height = Inches(8.5), Inches(11)
        s.left_margin = s.right_margin = Inches(1)
        s.top_margin = s.bottom_margin = Inches(1)
    style = doc.styles["Normal"]
    style.font.name, style.font.size = "Lexend", Pt(10)

    from docx.shared import RGBColor
    notice = doc.add_table(rows=1, cols=1)
    notice.style = "Table Grid"
    _set_widths(notice, [6.5])
    np_ = notice.rows[0].cells[0].paragraphs[0]
    nr = np_.add_run(
        "Generated on the website of Feigin and Fridman at https://PatentLawNY.com/patent-tools "
        "based on software programmed by AI - no warranty given - this document must be reviewed "
        "by a competent licensed Patent Attorney / Agent before submitting same to the "
        "U.S. Patent Office.")
    nr.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    nr.bold = True
    doc.add_paragraph()

    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = h.add_run("INFORMATION DISCLOSURE STATEMENT")
    r.bold = True
    r.font.size = Pt(13)

    info = doc.add_table(rows=0, cols=2)
    info.style = "Table Grid"
    for label, value in (("Applicant", applicant), ("Application No.", app_no),
                         ("Filing Date", filed), ("Title", title),
                         ("Examiner", examiner), ("Art Unit", art_unit),
                         ("Attorney Docket No.", docket)):
        if not value:
            continue
        row = info.add_row().cells
        row[0].text, row[1].text = label, value
        row[0].paragraphs[0].runs[0].bold = True
    doc.add_paragraph()

    n = 0
    for key, heading, cols in HEAD:
        group = [x for x in refs if x.table == key]
        if not group:
            continue
        p = doc.add_paragraph()
        p.add_run(heading).bold = True
        t = doc.add_table(rows=1, cols=len(cols))
        t.style = "Table Grid"
        for i, c in enumerate(cols):
            cell = t.rows[0].cells[i]
            cell.text = c
            cell.paragraphs[0].runs[0].bold = True
        for ref in sorted(group, key=lambda x: x.sort_key):
            n += 1
            cells = t.add_row().cells
            if key == "foreign":
                vals = [str(n), ref.country, ref.number, ref.kind, ref.dated or ref.filed, ref.inventor]
            elif key == "npl":
                vals = [str(n), ref.raw]
            else:
                vals = [str(n), ref.display_number(), ref.kind, ref.dated, ref.inventor]
            for i, v in enumerate(vals):
                cells[i].text = v or ""
            if key == "foreign":
                cells[-1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
                _checkbox(cells[-1].paragraphs[0], ref.translation_checked())
        _set_widths(t, WIDTHS[key])
        if key == "foreign":
            doc.add_paragraph("T: a checked box means an English-language translation "
                              "is provided with the reference.")
        doc.add_paragraph()

    doc.add_paragraph(
        "The undersigned submits the references listed above for the Examiner's "
        "consideration. No representation is made that a search has been conducted "
        "or that the information cited is material to patentability.")
    doc.add_paragraph()
    doc.add_paragraph("Respectfully submitted,")
    # The practitioner's signature block: signature, name and registration number
    # are left as lines to fill in (unless given); the date is the day it was made.
    from docx.enum.table import WD_ALIGN_VERTICAL
    block = doc.add_table(rows=0, cols=2)
    for label, value in (("Signature", ""), ("Name", attorney),
                         ("Registration No.", reg_no),
                         ("Date", signature_date(today))):
        row = block.add_row().cells
        row[0].text, row[1].text = label + ":", value
        row[0].paragraphs[0].runs[0].bold = True
        for c in row:
            c.vertical_alignment = WD_ALIGN_VERTICAL.BOTTOM
            c.paragraphs[0].paragraph_format.space_after = Pt(0)
            c.paragraphs[0].paragraph_format.space_before = Pt(8)
        if not value:   # a line to fill in by hand
            _underline_cell(row[1])
            block.rows[-1].height = Inches(0.5 if label == "Signature" else 0.35)
    _set_widths(block, [1.6, 3.4])
    doc.save(path)
    return n


def write_csv(refs, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["table", "number", "kind", "filing date", "date", "first inventor", "title", "note", "T"])
        for r in refs:
            w.writerow([r.table, r.display_number(), r.kind, r.filed, r.dated, r.inventor, r.title, r.note,
                        "yes" if r.translation_checked() else ""])


# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="ids-gen",
        description="Build an Information Disclosure Statement from a list of references. "
                    "Not legal advice: check every line before you file it.")
    ap.add_argument("numbers", nargs="*", help="patent or publication numbers")
    ap.add_argument("--from-folder", metavar="DIR", help="use the PDF file names in this folder")
    ap.add_argument("--from-file", metavar="FILE", help="one reference per line")
    ap.add_argument("-o", "--out", default="IDS.docx", help="Word file to write (default: IDS.docx)")
    ap.add_argument("--csv", metavar="FILE", help="also write what was found as a spreadsheet")
    ap.add_argument("--applicant", default=""), ap.add_argument("--app-no", default="")
    ap.add_argument("--filed", default="", help="filing date of YOUR application")
    ap.add_argument("--title", default=""), ap.add_argument("--docket", default="")
    ap.add_argument("--examiner", default="N/A"), ap.add_argument("--art-unit", default="N/A")
    ap.add_argument("--attorney", default="", help="name in the signature block")
    ap.add_argument("--reg-no", default="", help="USPTO registration number in the signature block "
                    "(left as a line to fill in when not given)")
    ap.add_argument("--no-lookup", action="store_true", help="do not go online; list the numbers as given")
    ap.add_argument("--source", choices=("auto", "google", "odp"), default="auto",
                    help="where to look the numbers up: Google Patents, the USPTO Open Data "
                         "Portal (needs a key), or auto = ODP when a key is set, else Google")
    ap.add_argument("--odp-key", default=os.environ.get("USPTO_ODP_KEY", ""),
                    help="USPTO Open Data Portal API key (default: $USPTO_ODP_KEY)")
    ap.add_argument("--old-db", metavar="FILE", default=os.environ.get("SIENNA_OLD_DB", ""),
                    help="offline index of pre-2001 US patents from build_old_db.py, used when the "
                         "portal or Google has nothing (default: $SIENNA_OLD_DB)")
    ap.add_argument("--no-google", action="store_true",
                    help="with --source odp, do not fall back to Google Patents for what the portal lacks")
    a = ap.parse_args(argv)

    refs = [Ref(x) for x in a.numbers]
    if a.from_file:
        refs += [Ref(l) for l in open(a.from_file) if l.strip() and not l.startswith("#")]
    if a.from_folder:
        refs += refs_from_folder(a.from_folder)
    if not refs:
        ap.error("give me some numbers, --from-file or --from-folder")

    seen, unique = set(), []
    for r in refs:
        k = (r.table, r.number.lower())
        if k in seen:
            continue
        seen.add(k)
        unique.append(r)

    source = a.source
    if source == "auto":
        source = "odp" if a.odp_key else "google"
    if source == "odp" and not a.odp_key:
        ap.error("--source odp needs --odp-key or $USPTO_ODP_KEY")
    if a.old_db and not os.path.exists(a.old_db):
        ap.error("--old-db: no such file: %s" % a.old_db)
    for r in unique:
        if not a.no_lookup:
            lookup(r, source, a.odp_key, a.old_db, google=not a.no_google)
        line = "  %-22s %-10s %-12s %s" % (r.display_number(), r.kind, r.dated or "-", r.inventor or "")
        print(line + (("   [" + r.note + "]") if r.note else ""))

    n = write_docx(unique, a.out, a.applicant, a.app_no, a.filed, a.title,
                   a.examiner, a.art_unit, a.attorney, a.docket, a.reg_no)
    print("\n%d reference(s) -> %s" % (n, a.out))
    if a.csv:
        write_csv(unique, a.csv)
        print("%d row(s) -> %s" % (len(unique), a.csv))
    missing = [r for r in unique if r.note]
    if missing:
        print("\nCheck by hand: %s" % ", ".join(r.display_number() for r in missing))
    checked = [r for r in unique if r.translation_checked()]
    if checked:
        print("\nT box checked, so provide a translation or clear the box: %s"
              % ", ".join(r.display_number() for r in checked))
    either = [r for r in unique if r.table == "foreign" and r.country in EITHER_LANGUAGE]
    if either:
        print("\nT box left clear, check the language: %s"
              % ", ".join(r.display_number() for r in either))
    return 0


if __name__ == "__main__":
    sys.exit(main())
