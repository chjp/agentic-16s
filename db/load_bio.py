#!/usr/bin/env python3
"""
Task 3.3b: load one pipeline result folder (DATA_DIR/pipeline/<PMID>/<tag>/tables/) into bio.*.

Usage:
  uv run --with 'psycopg[binary]' python db/load_bio.py ~/agentic16s-data/pipeline/23013615/main [--not-current]

Writes, in one transaction (see db/README.md, "Contract for the bioinformatics workflow"):
  bio.reference_db       upsert (name, version)
  bio.pipeline_run       one row; it becomes is_current for its protocol, and the old current run is superseded
  bio.run_qc             one row per samplesheet run
  bio.asv                global, deduplicated by sequence_md5
  bio.abundance          counts > 0
  bio.taxonomy_assignment  upsert per (asv, ref_db, classifier)
  core.run               processing_state, download_state, fastq_local_path, fastq_deleted_at
  ops.activity_log       one row

Idempotent: a folder already loaded (same study, protocol and samplesheet sha256) is replaced.
The tables/ folder is the durable copy, so a reload after `db/load_study.py` (which deletes the
study and, by cascade, its bio rows) restores everything.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate import database_url  # noqa: E402

LOADER = "script:db/load_bio.py"
csv.field_size_limit(10**9)


def read_tsv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def num(v):
    if v in (None, "", "NA"):
        return None
    return float(v)


def integer(v):
    return None if v in (None, "", "NA") else int(float(v))


def load(folder: Path, current: bool = True, conn=None) -> dict:
    t = folder / "tables"
    pr = json.loads((t / "pipeline_run.json").read_text())
    integrity = json.loads((t / "integrity.json").read_text())
    if not integrity["pass"]:
        raise SystemExit(f"integrity checks failed, not loading: {integrity['problems']}")
    qc = read_tsv(t / "run_qc.tsv")
    asv = read_tsv(t / "asv.tsv")
    ab = read_tsv(t / "abundance.tsv")
    tax = read_tsv(t / "taxonomy_assignment.tsv")
    pmid = pr["study_pmid"]
    label = pr["protocol_label"].split("_subset")[0]
    deleted_at = pr.get("raw_fastq_deleted_at")
    fastq_dir = folder.parent.parent.parent / "fastq" / pmid / pr["protocol_label"]

    own = conn is None
    conn = conn or psycopg.connect(database_url())
    counts = {}
    try:
        with conn.transaction():
            q = conn.execute
            row = q("select study_id from core.study where study_key like %s", (f"%-{pmid}",)).fetchone()
            if not row:
                raise SystemExit(f"study {pmid} is not in core.study; run db/load_study.py first")
            study_id = row[0]
            row = q("select protocol_id, variable_region from core.protocol where study_id = %s and label = %s",
                    (study_id, label)).fetchone()
            if not row:
                raise SystemExit(f"protocol {label!r} of {pmid} is not in core.protocol")
            protocol_id, region = row

            name, _, version = (pr.get("ref_db") or "").partition("=")
            ref_db_id = q("""insert into bio.reference_db (name, version) values (%s, %s)
                             on conflict (name, version) do update set name = excluded.name returning ref_db_id""",
                          (name, version)).fetchone()[0]

            # replace an earlier load of the same result folder
            q("""delete from bio.pipeline_run where study_id = %s and protocol_id = %s and samplesheet_sha256 = %s
                 and params->>'_protocol_label' = %s""",
              (study_id, protocol_id, pr["samplesheet_sha256"], pr["protocol_label"]))
            if current:
                q("""update bio.pipeline_run set is_current = false,
                     status = case when status = 'succeeded' then 'superseded' else status end
                     where protocol_id = %s and is_current""", (protocol_id,))
            prid = q("""insert into bio.pipeline_run (study_id, protocol_id, pipeline_name, pipeline_version, pipeline_commit,
                          nextflow_version, params, containers, ref_db_id, samplesheet_sha256, host, started_at, finished_at,
                          status, error_message, is_current, report_path)
                        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning pipeline_run_id""",
                     (study_id, protocol_id, pr["pipeline_name"], pr["pipeline_version"], pr.get("pipeline_commit"),
                      pr.get("nextflow_version"),
                      Jsonb({**(pr.get("params") or {}), "_param_reasons": pr.get("param_reasons"),
                             "_protocol_label": pr["protocol_label"], "_software_environment": pr.get("software_environment"),
                             "_software_versions": pr.get("software_versions"),
                             "_asv_orientation_flipped": pr.get("asv_orientation_flipped"),
                             "_fastq_source": pr.get("fastq_source"),
                             "_fastq_source_by_run": pr.get("fastq_source_by_run") or {}}),
                      Jsonb(pr.get("containers") or {}), ref_db_id, pr["samplesheet_sha256"], pr.get("host"),
                      pr.get("started_at"), pr.get("finished_at"), pr.get("status", "succeeded"), pr.get("error_message"),
                      current, pr.get("report_path"))).fetchone()[0]

            # runs must exist in core.run
            accs = [r["run_accession"] for r in qc]
            known = {r[0] for r in q("select run_accession from core.run where run_accession = any(%s)", (accs,))}
            missing = sorted(set(accs) - known)
            if missing:
                raise SystemExit(f"{len(missing)} runs not in core.run (e.g. {missing[0]})")

            with conn.cursor() as cur:
                cur.executemany("""insert into bio.run_qc (pipeline_run_id, run_accession, reads_input, pct_primer_found,
                                     reads_after_trim, reads_filtered, reads_denoised, reads_merged, reads_nonchim, status, failure_reason)
                                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                [(prid, r["run_accession"], integer(r["reads_input"]), num(r["pct_primer_found"]),
                                  integer(r["reads_after_trim"]), integer(r["reads_filtered"]), integer(r["reads_denoised"]),
                                  integer(r["reads_merged"]), integer(r["reads_nonchim"]), r["status"], r["failure_reason"] or None)
                                 for r in qc])
                cur.executemany("""insert into bio.asv (sequence, sequence_md5, length, region_inferred, first_pipeline_run_id)
                                   values (%s,%s,%s,%s,%s) on conflict (sequence_md5) do nothing""",
                                [(a["sequence"], a["sequence_md5"], int(a["length"]), region, prid) for a in asv])
            ids = dict(q("select sequence_md5, asv_id from bio.asv where sequence_md5 = any(%s)",
                         ([a["sequence_md5"] for a in asv],)).fetchall())
            with conn.cursor() as cur:
                with cur.copy("copy bio.abundance (pipeline_run_id, run_accession, asv_id, count) from stdin") as cp:
                    for a in ab:
                        cp.write_row((prid, a["run_accession"], ids[a["sequence_md5"]], int(a["count"])))
                cur.executemany("""insert into bio.taxonomy_assignment (asv_id, ref_db_id, classifier, domain, phylum, class,
                                     order_name, family, genus, species, confidence, min_bootstrap, pipeline_run_id)
                                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                   on conflict (asv_id, ref_db_id, classifier) do update set
                                     domain = excluded.domain, phylum = excluded.phylum, class = excluded.class,
                                     order_name = excluded.order_name, family = excluded.family, genus = excluded.genus,
                                     species = excluded.species, confidence = excluded.confidence,
                                     min_bootstrap = excluded.min_bootstrap, pipeline_run_id = excluded.pipeline_run_id""",
                                [(ids[x["sequence_md5"]], ref_db_id, x["classifier"], *(x[k] or None for k in
                                  ("domain", "phylum", "class", "order_name", "family", "genus", "species")),
                                  Jsonb(json.loads(x["confidence"] or "{}")), integer(x["min_bootstrap"]), prid) for x in tax])

            # core.run states (only for the run the analysis should use)
            if current:
                for r in qc:
                    ok = r["status"] in ("ok", "low_depth")
                    path = sorted(str(p) for p in fastq_dir.glob(f"fastq/*{r['run_accession']}*.fastq.gz")) or \
                        sorted(str(p) for p in fastq_dir.glob(f"*{r['run_accession']}*.fastq.gz"))
                    if path:
                        dstate, local, gone = "downloaded", ";".join(path), None
                    elif deleted_at and ok:
                        dstate, local, gone = "deleted", None, deleted_at
                    else:
                        dstate, local, gone = "not_downloaded", None, None
                    q("""update core.run set processing_state = %s, download_state = %s, fastq_local_path = %s,
                         fastq_deleted_at = %s where run_accession = %s""",
                      ("processed" if ok else "failed", dstate, local, gone, r["run_accession"]))

            counts = {"pipeline_run_id": prid, "run_qc": len(qc), "asv_in_run": len(asv), "abundance": len(ab),
                      "taxonomy": len(tax), "is_current": current}
            q("""insert into ops.activity_log (study_id, entity_type, entity_id, actor_type, actor, step, action, outcome, detail)
                 values (%s,'pipeline_run',%s,'script',%s,'pipeline','load pipeline results into bio tables','success',%s)""",
              (study_id, str(prid), LOADER, Jsonb({**counts, "folder": str(folder)})))
    finally:
        if own:
            conn.close()
    return counts


def mark_deleted(runs: list[str], when: str, conn=None) -> int:
    """After cleanup: processed runs whose FASTQ is gone. The DB rejects deleted without processed."""
    own = conn is None
    conn = conn or psycopg.connect(database_url())
    try:
        with conn.transaction():
            n = conn.execute("""update core.run set download_state = 'deleted', fastq_local_path = null, fastq_deleted_at = %s
                                where run_accession = any(%s) and processing_state = 'processed'""", (when, runs)).rowcount
    finally:
        if own:
            conn.close()
    return n


def check(folder: Path, conn=None) -> list[str]:
    """Per-run checks the cleanup stage requires before deleting FASTQ: returns problems (empty = ok)."""
    pr = json.loads((folder / "tables" / "pipeline_run.json").read_text())
    own = conn is None
    conn = conn or psycopg.connect(database_url())
    try:
        rows = conn.execute("""
            select q.run_accession, q.status, q.reads_input, r.spots, q.reads_nonchim,
                   coalesce((select sum(count) from bio.abundance a
                             where a.pipeline_run_id = q.pipeline_run_id and a.run_accession = q.run_accession), 0)
            from bio.run_qc q join bio.pipeline_run p using (pipeline_run_id) join core.run r using (run_accession)
            where p.is_current and p.samplesheet_sha256 = %s""", (pr["samplesheet_sha256"],)).fetchall()
    finally:
        if own:
            conn.close()
    problems = []
    if not rows:
        return ["no current pipeline run in the database for this samplesheet"]
    for acc, status, reads_input, spots, nonchim, total in rows:
        if status == "failed":
            continue
        if spots is not None and reads_input is not None and reads_input != spots:
            problems.append(f"{acc}: reads_input {reads_input} != archive spots {spots}")
        if (nonchim or 0) != total:
            problems.append(f"{acc}: abundance sum {total} != reads_nonchim {nonchim}")
    return problems


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="DATA_DIR/pipeline/<PMID>/<tag>")
    ap.add_argument("--not-current", action="store_true", help="load for inspection; don't make it the run analyses use")
    ap.add_argument("--check", action="store_true", help="only run the pre-deletion checks")
    ap.add_argument("--mark-deleted", action="store_true",
                    help="after cleanup: set download_state = deleted for processed runs (time from pipeline_run.json)")
    a = ap.parse_args()
    folder = a.folder.expanduser().resolve()
    if a.mark_deleted:
        pr = json.loads((folder / "tables" / "pipeline_run.json").read_text())
        runs = [r["run_accession"] for r in read_tsv(folder / "tables" / "run_qc.tsv") if r["status"] != "failed"]
        print(f"{mark_deleted(runs, pr['raw_fastq_deleted_at'])} runs marked deleted")
        sys.exit(0)
    if a.check:
        p = check(folder)
        print("\n".join(p) or "ok")
        sys.exit(1 if p else 0)
    print(json.dumps(load(folder, current=not a.not_current), indent=2))
