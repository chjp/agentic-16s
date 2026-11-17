#!/usr/bin/env python3
"""
Fill core.run.fastq_urls / fastq_md5 / fastq_bytes from the ENA portal API for every project
in the database, keeping the raw response in raw.api_record.

Usage:  uv run --with 'psycopg[binary]' python db/fetch_fastq_links.py
"""
import csv
import hashlib
import io
import sys
import time
import urllib.request
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate import database_url  # noqa: E402

URL = ("https://www.ebi.ac.uk/ena/portal/api/filereport?accession={acc}&result=read_run"
       "&fields=run_accession,fastq_ftp,fastq_md5,fastq_bytes&format=tsv")


def main():
    with psycopg.connect(database_url()) as conn:
        projects = [r[0] for r in conn.execute("select project_accession from core.archive_project order by 1")]
        for acc in projects:
            url = URL.format(acc=acc)
            req = urllib.request.Request(url, headers={"User-Agent": "agentic-16s"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    text, status = resp.read().decode("utf-8"), resp.status
            except Exception as e:  # noqa: BLE001
                print(f"{acc}: failed ({e})")
                conn.execute("""insert into ops.activity_log (actor_type, actor, step, action, input, outcome, reason)
                                values ('script','script:db/fetch_fastq_links.py','archive','fetch ENA fastq links',%s,'failure',%s)""",
                             (psycopg.types.json.Jsonb({"url": url}), str(e)))
                continue
            rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
            with conn.transaction():
                conn.execute("""insert into raw.api_record (source, accession, request, http_status, content_type, payload_text, payload_sha256)
                                values ('ena_filereport', %s, %s, %s, 'tsv', %s, %s)""",
                             (acc, url, status, text, hashlib.sha256(text.encode()).hexdigest()))
                n = 0
                for r in rows:
                    if not r.get("fastq_ftp"):
                        continue
                    urls = ["https://" + u for u in r["fastq_ftp"].split(";") if u]
                    md5 = [m for m in r.get("fastq_md5", "").split(";") if m]
                    size = [int(b) for b in r.get("fastq_bytes", "").split(";") if b.isdigit()]
                    cur = conn.execute("""update core.run set fastq_urls = %s, fastq_md5 = %s, fastq_bytes = %s
                                          where run_accession = %s""", (urls, md5 or None, size or None, r["run_accession"]))
                    n += cur.rowcount
                conn.execute("""insert into ops.activity_log (actor_type, actor, step, action, input, outcome, detail)
                                values ('script','script:db/fetch_fastq_links.py','archive','fetch ENA fastq links',%s,'success',%s)""",
                             (psycopg.types.json.Jsonb({"url": url}),
                              psycopg.types.json.Jsonb({"runs_in_report": len(rows), "runs_updated": n})))
            print(f"{acc}: {len(rows)} runs in ENA, {n} updated")
            time.sleep(0.5)


if __name__ == "__main__":
    main()
