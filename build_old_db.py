#!/usr/bin/env python3
"""build_old_db.py -- an offline index of the US patents the Open Data Portal does not have.

The USPTO's Open Data Portal (what `ids-gen --source odp` reads) only knows
applications filed on or after 1 January 2001. For anything older it answers
"not found", and an IDS full of 1990s references comes back with blank dates.

This builds a small SQLite file from PatentsView's bulk grant data (1976 on),
which the USPTO now rehosts on the same portal as product PVGPATDIS. Three of
its files are needed; download them by hand or with --odp-key (the signed
download link the portal hands out lives for about a minute, so the script
fetches each file the moment it gets the link):

    g_patent.tsv.zip                  number, grant date, title, kind
    g_application.tsv.zip             filing date
    g_inventor_disambiguated.tsv.zip  first named inventor

    build_old_db.py --odp-key $USPTO_ODP_KEY --out old_patents.sqlite
    build_old_db.py --from-dir ~/Downloads/pvgpatdis --out old_patents.sqlite
    ids-gen --source odp --old-db old_patents.sqlite 5368281 5518486

Only patents filed before 2001 (or with no filing date on record) are kept, so
the file stays around a couple of hundred megabytes rather than a gigabyte.
Pass --all to keep every grant. The portal allows 20 downloads of each file per
year, so keep the zips once you have them.
"""

import argparse, csv, io, os, re, sqlite3, sys, time, urllib.request, zipfile

FILES = ("g_patent.tsv.zip", "g_application.tsv.zip", "g_inventor_disambiguated.tsv.zip")
PRODUCT = "https://api.uspto.gov/api/v1/datasets/products/files/PVGPATDIS/%s"
CUTOFF = "2001-01-01"           # the portal's first filing date


def download(name, key, into):
    """Ask the portal for the signed link, then fetch the file straight away."""
    path = os.path.join(into, name)
    if os.path.exists(path) and zipfile.is_zipfile(path):
        print("  have", name)
        return path
    req = urllib.request.Request(PRODUCT % name, headers={"X-API-KEY": key})
    body = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    m = re.search(r'https://data\.uspto\.gov/files/PVGPATDIS/[^\s"]+', body)
    if not m:
        sys.exit("no download link for %s: %s" % (name, body[:300]))
    url = m.group(0).rstrip(".")
    print("  fetching", name)
    with urllib.request.urlopen(url, timeout=7200) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    if not zipfile.is_zipfile(path):
        sys.exit("%s is not a zip file -- the signed link may have expired; run again" % name)
    return path


def rows(zip_path):
    """The one TSV inside a PatentsView zip, as dicts."""
    with zipfile.ZipFile(zip_path) as z:
        inner = [n for n in z.namelist() if n.lower().endswith(".tsv")][0]
        with z.open(inner) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
            yield from csv.DictReader(text, delimiter="\t", quotechar='"')


def load(con, table, zip_path, cols, pick):
    """Stream one TSV into a scratch table, a few thousand rows at a time, so the
    build never holds the data in memory (the inventor file alone is 25M rows)."""
    con.execute("CREATE TABLE %s (%s)" % (table, ", ".join(cols)))
    sql = "INSERT OR IGNORE INTO %s VALUES (%s)" % (table, ",".join("?" * len(cols)))
    batch, n = [], 0
    for r in rows(zip_path):
        v = pick(r)
        if v is None:
            continue
        batch.append(v)
        if len(batch) >= 20000:
            con.executemany(sql, batch); n += len(batch); batch = []
    con.executemany(sql, batch); n += len(batch)
    con.commit()
    print("  %d rows" % n, flush=True)


def build(d, out, keep_all=False):
    t0 = time.time()
    work = out + ".work"
    for f in (out, work):
        if os.path.exists(f):
            os.remove(f)
    con = sqlite3.connect(work)
    con.execute("PRAGMA journal_mode=OFF"); con.execute("PRAGMA synchronous=OFF")
    clean = lambda x: (x or "").strip()

    print("filing dates ...", flush=True)
    load(con, "app", os.path.join(d, "g_application.tsv.zip"), ["number TEXT PRIMARY KEY", "filed TEXT"],
         lambda r: (clean(r["patent_id"]), clean(r.get("filing_date"))) if clean(r["patent_id"]) else None)
    print("patents ...", flush=True)
    load(con, "pat", os.path.join(d, "g_patent.tsv.zip"),
         ["number TEXT PRIMARY KEY", "kind TEXT", "dated TEXT", "title TEXT"],
         lambda r: (clean(r["patent_id"]), clean(r.get("wipo_kind")), clean(r.get("patent_date")),
                    re.sub(r"\s+", " ", clean(r.get("patent_title")))) if clean(r["patent_id"]) else None)
    print("inventors ...", flush=True)
    load(con, "inv", os.path.join(d, "g_inventor_disambiguated.tsv.zip"), ["number TEXT PRIMARY KEY", "inventor TEXT"],
         lambda r: (clean(r["patent_id"]), ("%s %s" % (clean(r.get("disambig_inventor_name_first")),
                                                       clean(r.get("disambig_inventor_name_last")))).strip())
         if clean(r.get("inventor_sequence")) in ("0", "") else None)

    print("joining ...", flush=True)
    con.execute("CREATE TABLE patents (number TEXT PRIMARY KEY, kind TEXT, filed TEXT, "
                "dated TEXT, inventor TEXT, title TEXT)")
    con.execute("INSERT INTO patents SELECT pat.number, pat.kind, COALESCE(app.filed, ''), pat.dated, "
                "COALESCE(inv.inventor, ''), pat.title FROM pat LEFT JOIN app ON app.number = pat.number "
                "LEFT JOIN inv ON inv.number = pat.number" +
                ("" if keep_all else " WHERE app.filed IS NULL OR app.filed = '' OR app.filed < ?"),
                () if keep_all else (CUTOFF,))
    n = con.execute("SELECT count(*) FROM patents").fetchone()[0]
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("built", time.strftime("%Y-%m-%d")), ("source", "PatentsView PVGPATDIS via data.uspto.gov"),
        ("rows", str(n)), ("cutoff", "all" if keep_all else "filed before " + CUTOFF)])
    for t in ("app", "pat", "inv"):
        con.execute("DROP TABLE " + t)
    con.commit()
    con.execute("VACUUM INTO ?", (out,))
    con.close()
    os.remove(work)
    print("  %d patents -> %s, %.0f MB, %.0f s" % (n, out, os.path.getsize(out) / 1e6, time.time() - t0), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-dir", default=".", help="folder holding the three PatentsView zips (default: here)")
    ap.add_argument("--odp-key", default=os.environ.get("USPTO_ODP_KEY", ""),
                    help="download the zips first with this Open Data Portal key")
    ap.add_argument("--out", default="old_patents.sqlite")
    ap.add_argument("--all", action="store_true", help="keep every grant, not just those filed before 2001")
    a = ap.parse_args(argv)
    os.makedirs(a.from_dir, exist_ok=True)
    if a.odp_key:
        print("downloading into", a.from_dir)
        for n in FILES:
            download(n, a.odp_key, a.from_dir)
    for n in FILES:
        if not os.path.exists(os.path.join(a.from_dir, n)):
            ap.error("%s is missing from %s (pass --odp-key to download it)" % (n, a.from_dir))
    build(a.from_dir, a.out, a.all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
