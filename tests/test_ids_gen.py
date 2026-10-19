"""Offline tests: the classifying, the sorting and the document itself."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ids_gen


class Classify(unittest.TestCase):
    def kinds(self, *pairs):
        for raw, table in pairs:
            self.assertEqual(ids_gen.Ref(raw).table, table, raw)

    def test_us_patents(self):
        self.kinds(("10000000", "us_patent"), ("US9,876,543 B2", "us_patent"), ("7000000", "us_patent"))

    def test_publications(self):
        self.kinds(("2015/0123456", "us_pub"), ("US20150123456A1", "us_pub"), ("2015-0123456", "us_pub"))

    def test_foreign_and_literature(self):
        self.kinds(("EP1234567A1", "foreign"), ("WO 2019/123456", "foreign"),
                   ("Jones, A Survey of Widget Fasteners, 2019", "npl"))

    def test_number_shapes(self):
        r = ids_gen.Ref("US9,876,543 B2")
        self.assertEqual(r.number, "9876543")
        self.assertEqual(r.kind, "B2")
        self.assertEqual(r.display_number(), "9,876,543")
        self.assertEqual(ids_gen.Ref("2015/123456").number, "2015/0123456")
        self.assertEqual(ids_gen.Ref("EP 1 234 567 A1").country, "EP")

    def test_google_id(self):
        self.assertEqual(ids_gen.Ref("10000000").google_id(), "US10000000")
        self.assertEqual(ids_gen.Ref("2015/0123456").google_id(), "US20150123456A1")
        self.assertIsNone(ids_gen.Ref("Jones 2019, widgets").google_id())


class Document(unittest.TestCase):
    def test_writes_every_table_and_numbers_straight_through(self):
        refs = [ids_gen.Ref(x) for x in
                ("10000000", "9876543", "2015/0123456", "EP1234567A1", "Jones 2019, widgets")]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "IDS.docx")
            n = ids_gen.write_docx(refs, out, applicant="Smith LLC", app_no="19/801,543")
            self.assertEqual(n, 5)
            self.assertTrue(os.path.getsize(out) > 5000)
            from docx import Document as Docx
            doc = Docx(out)
            headings = [p.text for p in doc.paragraphs]
            self.assertIn("U.S. PATENTS", headings)
            self.assertIn("FOREIGN PATENT DOCUMENTS", headings)
            self.assertIn("NON-PATENT LITERATURE DOCUMENTS", headings)
            cites = [t.rows[i].cells[0].text for t in doc.tables[2:-1] for i in range(1, len(t.rows))]   # 0 notice, 1 case info, last signature block
            self.assertEqual(cites, ["1", "2", "3", "4", "5"])   # one run of numbers

    def test_signature_block_dates_itself_and_leaves_lines_to_fill(self):
        import datetime
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "IDS.docx")
            ids_gen.write_docx([ids_gen.Ref("10000000")], out, today=datetime.date(2026, 10, 19))
            from docx import Document as Docx
            rows = {r.cells[0].text: r.cells[1].text for r in Docx(out).tables[-1].rows}
            self.assertEqual(rows["Date:"], "October 19, 2026")
            self.assertEqual(rows["Signature:"], "")
            self.assertEqual((rows["Name:"], rows["Registration No.:"]), ("", ""))   # underlined blanks
            ids_gen.write_docx([ids_gen.Ref("10000000")], out, attorney="Jane Roe", reg_no="12,345")
            rows = {r.cells[0].text: r.cells[1].text for r in Docx(out).tables[-1].rows}
            self.assertEqual((rows["Name:"], rows["Registration No.:"]), ("Jane Roe", "12,345"))
            self.assertEqual(rows["Date:"], ids_gen.signature_date())

    def test_csv(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "refs.csv")
            ids_gen.write_csv([ids_gen.Ref("10000000")], out)
            self.assertIn("10,000,000", open(out).read())


class Folder(unittest.TestCase):
    def test_reads_numbers_out_of_file_names(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("US10000000B2.pdf", "us 9,876,543.pdf", "notes.txt"):
                open(os.path.join(d, name), "w").close()
            refs = ids_gen.refs_from_folder(d)
            self.assertEqual(len(refs), 2)
            self.assertEqual(sorted(r.number for r in refs), ["10000000", "9876543"])




class FileNames(unittest.TestCase):
    def test_a_word_after_the_number_is_not_a_kind_code(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("US5518486-see Figure 5B.pdf", "US6152311A magnetically attachable.pdf",
                      "US10000000B2.pdf", "!US11395437 magnetic faceplate.pdf"):
                open(os.path.join(d, n), "w").close()
            got = {r.number: r.kind for r in ids_gen.refs_from_folder(d)}
            self.assertEqual(got, {"5518486": "", "6152311": "A", "10000000": "B2", "11395437": ""})


class LocalIndex(unittest.TestCase):
    def index(self, d):
        import sqlite3
        path = os.path.join(d, "old.sqlite")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE patents (number TEXT PRIMARY KEY, kind TEXT, filed TEXT, "
                    "dated TEXT, inventor TEXT, title TEXT)")
        con.execute("INSERT INTO patents VALUES ('5368281','A','1993-01-11','1994-11-29','Frank Ferrara','Hand grip')")
        con.commit(); con.close()
        return path

    def test_fills_a_pre_2001_patent_from_the_index(self):
        with tempfile.TemporaryDirectory() as d:
            r = ids_gen.Ref("5,368,281")
            r.note = "nothing found -- check the number"
            ids_gen.fetch_local(r, self.index(d))
            self.assertEqual((r.dated, r.filed, r.inventor, r.note), ("1994-11-29", "1993-01-11", "Frank Ferrara", ""))

    def test_leaves_an_unknown_number_and_its_note_alone(self):
        with tempfile.TemporaryDirectory() as d:
            r = ids_gen.Ref("5,518,486")
            r.note = "nothing found -- check the number"
            ids_gen.fetch_local(r, self.index(d))
            self.assertEqual((r.dated, r.note), ("", "nothing found -- check the number"))

    def test_lookup_chain_falls_through_to_the_index(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.index(d)
            calls = []
            def no_odp(ref, key, pause=0):
                calls.append("odp"); ref.note = "nothing found -- check the number"; return ref
            def no_google(ref, pause=0):
                calls.append("google"); return ref
            orig = ids_gen.fetch_odp, ids_gen.fetch
            ids_gen.fetch_odp, ids_gen.fetch = no_odp, no_google
            try:
                r = ids_gen.lookup(ids_gen.Ref("5368281"), "odp", "key", path, google=False)
                self.assertEqual(r.dated, "1994-11-29")
                self.assertEqual(calls, ["odp"])
                r = ids_gen.lookup(ids_gen.Ref("5518486"), "odp", "key", path, google=True)
                self.assertEqual(calls, ["odp", "odp", "google"])
                self.assertEqual(r.note, "nothing found -- check the number")
            finally:
                ids_gen.fetch_odp, ids_gen.fetch = orig


if __name__ == "__main__":
    unittest.main()
