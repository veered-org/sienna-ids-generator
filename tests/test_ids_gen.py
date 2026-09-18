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
            cites = [t.rows[i].cells[0].text for t in doc.tables[1:] for i in range(1, len(t.rows))]
            self.assertEqual(cites, ["1", "2", "3", "4", "5"])   # one run of numbers

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


if __name__ == "__main__":
    unittest.main()
