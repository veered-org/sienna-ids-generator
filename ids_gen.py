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

import argparse, csv, os, re, sys, time, urllib.parse, urllib.request

UA = "Mozilla/5.0 (ids-gen; +https://veered.org/ids-generator/)"
GP = "https://patents.google.com/patent/%s/en"

# --------------------------------------------------------------------------- #
#  Reading the numbers                                                         #
# --------------------------------------------------------------------------- #

US_PUB = re.compile(r"^(?:US)?\s*(20\d{2})[/\-]?(\d{6,7})\s*([A-Z]\d?)?$", re.I)
US_PAT = re.compile(r"^(?:US)?\s*([0-9]{1,2},?[0-9]{3},?[0-9]{3})\s*([A-Z]\d?)?$", re.I)
FOREIGN = re.compile(r"^([A-Z]{2})\s*([0-9][0-9 ,/\-]*)\s*([A-Z]\d?)?$", re.I)


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


def refs_from_folder(folder):
    """Every PDF in a folder, read as 'the file name is the number'."""
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".pdf"):
            continue
        stem = os.path.splitext(name)[0]
        stem = re.sub(r"[_\-]+", " ", stem)
        m = re.search(r"((?:US|EP|WO|GB|DE|FR|JP|CN|KR|CA|IL)?\s*"
                      r"(?:20\d{2}[/\-]?\d{6,7}|[0-9]{1,2},?[0-9]{3},?[0-9]{3})\s*[A-Z]?\d?)", stem, re.I)
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


# --------------------------------------------------------------------------- #
#  Writing the document                                                        #
# --------------------------------------------------------------------------- #

HEAD = [("us_patent", "U.S. PATENTS",
         ["Cite No.", "Patent Number", "Kind Code", "Issue Date", "Name of Patentee"]),
        ("us_pub", "U.S. PATENT APPLICATION PUBLICATIONS",
         ["Cite No.", "Publication Number", "Kind Code", "Publication Date", "Name of Applicant"]),
        ("foreign", "FOREIGN PATENT DOCUMENTS",
         ["Cite No.", "Country", "Number", "Kind Code", "Date", "Name of Applicant"]),
        ("npl", "NON-PATENT LITERATURE DOCUMENTS",
         ["Cite No.", "Document"])]


def write_docx(refs, path, applicant="", app_no="", filed="", title="",
               examiner="N/A", art_unit="N/A", attorney="", docket=""):
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
        doc.add_paragraph()

    doc.add_paragraph(
        "The undersigned submits the references listed above for the Examiner's "
        "consideration. No representation is made that a search has been conducted "
        "or that the information cited is material to patentability.")
    doc.add_paragraph()
    sign = doc.add_paragraph()
    sign.add_run("Respectfully submitted,\n\n\n")
    sign.add_run((attorney or "________________________") + "\n")
    doc.save(path)
    return n


def write_csv(refs, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["table", "number", "kind", "filing date", "date", "first inventor", "title", "note"])
        for r in refs:
            w.writerow([r.table, r.display_number(), r.kind, r.filed, r.dated, r.inventor, r.title, r.note])


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
    ap.add_argument("--attorney", default="")
    ap.add_argument("--no-lookup", action="store_true", help="do not go online; list the numbers as given")
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

    for r in unique:
        if not a.no_lookup:
            fetch(r)
        line = "  %-22s %-10s %-12s %s" % (r.display_number(), r.kind, r.dated or "-", r.inventor or "")
        print(line + (("   [" + r.note + "]") if r.note else ""))

    n = write_docx(unique, a.out, a.applicant, a.app_no, a.filed, a.title,
                   a.examiner, a.art_unit, a.attorney, a.docket)
    print("\n%d reference(s) -> %s" % (n, a.out))
    if a.csv:
        write_csv(unique, a.csv)
        print("%d row(s) -> %s" % (len(unique), a.csv))
    missing = [r for r in unique if r.note]
    if missing:
        print("\nCheck by hand: %s" % ", ".join(r.display_number() for r in missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
