"""Task 3.3a: ampliseq output -> tables shaped like the draft bio.* tables (schema-design.html).

  tables/pipeline_run.json        bio.pipeline_run (one object)
  tables/run_qc.tsv               bio.run_qc
  tables/asv.tsv                  bio.asv
  tables/abundance.tsv            bio.abundance (long format, nonzero counts only)
  tables/taxonomy_assignment.tsv  bio.taxonomy_assignment
  tables/integrity.json           completeness checks; cleanup deletes FASTQ only if they all pass

Keys are run accessions and ASV sequence md5s, so the files can be COPYed into PostgreSQL
once Phase 1 exists (task 3.3b).
"""
from __future__ import annotations

import argparse
import glob
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules; python3 -I drops this
import common  # noqa: E402

LOW_DEPTH = 1000
LOW_FILTER_RETENTION = 0.20   # flag a run whose quality filter keeps less than this share
RANKS = {"domain": "domain", "kingdom": "domain", "phylum": "phylum", "class": "class", "order": "order_name",
         "family": "family", "genus": "genus", "species": "species"}


def read_fasta(path: Path) -> dict[str, str]:
    out, name = {}, None
    for line in path.read_text().splitlines():
        if line.startswith(">"):
            name = line[1:].split()[0]
            out[name] = ""
        elif name:
            out[name] += line.strip().upper()
    return out


def as_int(v) -> int | None:
    """Counts as ampliseq writes them, including thousands separators ('77,595')."""
    try:
        return int(float(str(v).replace(",", "").rstrip("%")))
    except (TypeError, ValueError):
        return None


def by_sample(rows: list[dict]) -> dict[str, dict]:
    if not rows:
        return {}
    key = "sample" if "sample" in rows[0] else next(iter(rows[0]))
    return {r[key]: r for r in rows}


def parse_versions_yml(path: Path) -> dict:
    """nf-core software_versions YAML (process: {tool: version}) without a YAML library."""
    out: dict[str, dict] = {}
    current = None
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, val = line.strip().partition(":")
        key, val = key.strip().strip('"'), val.strip().strip('"')
        if not line.startswith((" ", "\t")):
            current = key
            out[current] = {}
        elif current is not None:
            out[current][key] = val
    return out


def taxonomy_file(dada2: Path) -> Path | None:
    files = sorted(Path(p) for p in glob.glob(str(dada2 / "ASV_tax*.tsv")))
    species = [f for f in files if "species" in f.name]
    return (species or files or [None])[0]


def collect(pmid: str, label: str, meta: dict | None = None) -> dict:
    d = common.protocol_dir(pmid, label)
    res = d / "ampliseq"
    out = d / "tables"
    out.mkdir(exist_ok=True)
    params = json.loads((d / "params.json").read_text())
    flip = params.get("observed", {}).get("orientation") == "reverse"
    samplesheet = common.read_tsv(d / "samplesheet.tsv")
    runs = [r["sample"] for r in samplesheet]

    # --- ASVs (flipped to gene orientation when the reads were sequenced from the reverse primer)
    seqs = read_fasta(res / "dada2" / "ASV_seqs.fasta")
    id_to_md5, asv_rows = {}, {}
    for asv_id, s in seqs.items():
        seq = common.revcomp(s) if flip else s
        m = common.md5(seq)
        id_to_md5[asv_id] = m
        asv_rows[m] = {"sequence_md5": m, "sequence": seq, "length": len(seq)}
    common.write_tsv(out / "asv.tsv", list(asv_rows.values()), ["sequence_md5", "sequence", "length"])

    # --- abundance, long format
    table = common.read_tsv(res / "dada2" / "ASV_table.tsv")
    abundance, run_sum = [], {}
    for row in table:
        m = id_to_md5[row["ASV_ID"]]
        for run, v in row.items():
            if run == "ASV_ID":
                continue
            c = as_int(v) or 0
            if c > 0:
                abundance.append({"run_accession": run, "sequence_md5": m, "count": c})
                run_sum[run] = run_sum.get(run, 0) + c
    common.write_tsv(out / "abundance.tsv", abundance, ["run_accession", "sequence_md5", "count"])

    # --- taxonomy
    tax_rows = []
    tf = taxonomy_file(res / "dada2")
    ref = (params["params"].get("dada_ref_taxonomy") or "").replace("=", "_")
    min_boot = params["params"].get("dada_min_boot", 50)
    if tf:
        for r in common.read_tsv(tf):
            m = id_to_md5.get(r["ASV_ID"])
            if m is None:
                continue
            row = {"sequence_md5": m, "ref_db": ref, "classifier": f"dada2_assigntaxonomy ({tf.name})",
                   "min_bootstrap": min_boot}
            conf = {}
            for col, v in r.items():
                c = col.lower()
                if c in RANKS:
                    row[RANKS[c]] = v or ""
                elif c.endswith("_confidence") and v not in ("", "NA"):
                    conf[RANKS.get(c[:-11], c[:-11])] = float(v)
            row["confidence"] = json.dumps(conf, sort_keys=True)
            tax_rows.append(row)
    common.write_tsv(out / "taxonomy_assignment.tsv", tax_rows,
                     ["sequence_md5", "ref_db", "classifier", "domain", "phylum", "class", "order_name", "family",
                      "genus", "species", "confidence", "min_bootstrap"])

    # --- run_qc
    stats = by_sample(common.read_tsv(res / "dada2" / "DADA2_stats.tsv"))
    cut_path = res / "cutadapt" / "cutadapt_summary.tsv"
    cut = by_sample(common.read_tsv(cut_path)) if cut_path.exists() else {}
    qc = []
    for run in runs:
        s, c = stats.get(run, {}), cut.get(run, {})
        reads_input = as_int(c.get("cutadapt_total_processed")) or as_int(s.get("DADA2_input"))
        nonchim = as_int(s.get("nonchim"))
        denoised = as_int(s.get("denoisedPairs")) or as_int(s.get("denoised")) or as_int(s.get("denoisedF"))
        row = {"run_accession": run, "reads_input": reads_input,
               "pct_primer_found": c.get("cutadapt_passing_filters_percent", "").rstrip("%") if c else "",
               "reads_after_trim": as_int(c.get("cutadapt_passing_filters")) if c else as_int(s.get("DADA2_input")),
               "reads_filtered": as_int(s.get("filtered")), "reads_denoised": denoised,
               "reads_merged": as_int(s.get("merged")), "reads_nonchim": nonchim}
        if not s:
            row.update(status="failed", failure_reason="absent from DADA2 stats (no reads left after trimming or filtering)")
        elif not nonchim:
            row.update(status="failed", failure_reason="0 non-chimeric reads")
        elif nonchim < LOW_DEPTH:
            row.update(status="low_depth", failure_reason=f"{nonchim} non-chimeric reads (< {LOW_DEPTH})")
        else:
            row.update(status="ok", failure_reason="")
        kept = (row["reads_filtered"] or 0) / row["reads_after_trim"] if row["reads_after_trim"] else None
        if kept is not None and kept < LOW_FILTER_RETENTION:
            note = f"quality filtering kept only {kept:.0%} of reads (maxEE/truncation)"
            row["failure_reason"] = "; ".join(x for x in (row["failure_reason"], note) if x)
        qc.append(row)
    common.write_tsv(out / "run_qc.tsv", qc, ["run_accession", "reads_input", "pct_primer_found", "reads_after_trim",
                                              "reads_filtered", "reads_denoised", "reads_merged", "reads_nonchim",
                                              "status", "failure_reason"])

    # --- pipeline_run
    info = res / "pipeline_info"
    ampliseq_params = {}
    pfiles = sorted(info.glob("params_*.json"))
    if pfiles:
        ampliseq_params = json.loads(pfiles[-1].read_text())
    containers = {}
    for tr in sorted(info.glob("execution_trace_*.txt")):
        for r in common.read_tsv(tr):
            if r.get("container") and r.get("name") and r["container"] != "-":
                containers[r["name"].split(" (")[0]] = r["container"]
    vfiles = sorted(info.glob("*software*versions*.yml"))
    software = parse_versions_yml(vfiles[0]) if vfiles else {}
    meta = meta or {}
    v = common.versions()
    pr = {
        "pipeline_name": "nf-core/ampliseq", "pipeline_version": v.get("AMPLISEQ_VERSION"),
        "pipeline_commit": meta.get("pipeline_commit"), "wrapper": "pipeline/bin/run_study.py",
        "nextflow_version": v.get("NXF_VER"), "study_pmid": pmid, "protocol_label": label,
        "params": ampliseq_params or params["params"], "param_reasons": params["reasons"],
        "containers": containers, "software_environment": v.get("NF_PROFILE"), "software_versions": software,
        "ref_db": params["params"].get("dada_ref_taxonomy"),
        "samplesheet_sha256": common.sha256_file(d / "samplesheet.tsv"), "host": socket.gethostname(),
        "started_at": meta.get("started_at"), "finished_at": meta.get("finished_at"),
        "status": meta.get("status", "succeeded"), "error_message": meta.get("error_message"),
        "asv_orientation_flipped": flip,
        "fastq_source": params.get("observed", {}).get("fastq_source", "ena_fastq"),
        # runs whose ENA FASTQ could not be downloaded and came from NCBI instead (run_study.fetch_ena_fallback)
        "fastq_source_by_run": {r: {k: x.get(k) for k in ("source", "reason", "sra_md5", "url")}
                                for r, x in (json.loads((d / "fastq_fallback.json").read_text())
                                             if (d / "fastq_fallback.json").exists() else {}).items()},
        "report_path": str((res / "multiqc" / "multiqc_report.html").relative_to(common.data_dir())),
        "raw_fastq_deleted_at": None,
    }
    old = out / "pipeline_run.json"
    if old.exists():  # re-collecting after cleanup must not forget that the reads are gone
        pr["raw_fastq_deleted_at"] = json.loads(old.read_text()).get("raw_fastq_deleted_at")
    common.write_json(out / "pipeline_run.json", pr)

    # --- integrity (gate for deleting raw FASTQ)
    problems = []
    if meta.get("exit_code", 0) != 0:
        problems.append(f"ampliseq exit code {meta.get('exit_code')}")
    missing = [r for r in runs if r not in {q['run_accession'] for q in qc}]
    if missing:
        problems.append(f"{len(missing)} samplesheet runs without a run_qc row")
    mism = [r for r, s in stats.items() if (as_int(s.get("nonchim")) or 0) != run_sum.get(r, 0)]
    if mism:
        problems.append(f"{len(mism)} runs where abundance sum != DADA2 nonchim (e.g. {mism[0]})")
    bad_md5 = [m for m, a in asv_rows.items() if common.md5(a["sequence"]) != m]
    if bad_md5:
        problems.append(f"{len(bad_md5)} ASVs whose md5 does not match the sequence")
    if not flip:
        bad_ids = [i for i, m in id_to_md5.items() if i != m]
        if bad_ids:
            problems.append(f"{len(bad_ids)} ampliseq ASV_IDs differ from md5(sequence)")
    tax_missing = len(asv_rows) - len({t['sequence_md5'] for t in tax_rows})
    if tax_missing:
        problems.append(f"{tax_missing} ASVs without a taxonomy row")
    integrity = {"pass": not problems, "problems": problems, "n_runs": len(runs), "n_asv": len(asv_rows),
                 "n_abundance_rows": len(abundance), "n_taxonomy_rows": len(tax_rows)}
    common.write_json(out / "integrity.json", integrity)
    common.log_event(pmid, "collect ampliseq results", "success" if not problems else "partial",
                     f"protocol {label}: {len(runs)} runs, {len(asv_rows)} ASVs, {len(abundance)} abundance rows; "
                     f"integrity {'pass' if not problems else 'FAIL: ' + '; '.join(problems)}",
                     source=str(res), files=sorted(out.glob("*")))
    return integrity


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pmid")
    ap.add_argument("--protocol", default="main")
    a = ap.parse_args()
    print(json.dumps(collect(a.pmid, a.protocol), indent=2))
