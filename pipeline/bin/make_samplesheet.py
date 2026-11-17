"""Task 3.1: run list and ampliseq samplesheet for one study.

Two steps:
  prepare   16S runs of the study + ENA file report  ->  manifest.tsv, runs.csv (one per protocol)
  finalize  fetchngs output                            ->  samplesheet.tsv for ampliseq

The run source is load_runs(). Today it reads studies/<PMID>/sample_sheet.tsv; once the
Phase 1 database exists it becomes a query on core.run joined to core.protocol.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import csv
import gzip
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules; python3 -I drops this
import common  # noqa: E402

ENA_FIELDS = [
    "run_accession", "sample_accession", "study_accession", "library_layout", "library_strategy",
    "instrument_platform", "instrument_model", "read_count", "base_count",
    "fastq_ftp", "fastq_md5", "fastq_bytes",
]
ENA_URL = "https://www.ebi.ac.uk/ena/portal/api/filereport"


# ---------------------------------------------------------------- ENA

def ena_filereport(pmid: str, project: str) -> dict[str, dict]:
    """ENA read_run report for a project, cached under sources/archive/ (raw layer)."""
    cache = common.STUDIES / pmid / "sources" / "archive" / f"ena_filereport_pipeline_{project}.tsv"
    if not cache.exists():
        q = urllib.parse.urlencode({"accession": project, "result": "read_run",
                                    "fields": ",".join(ENA_FIELDS), "format": "tsv"})
        req = urllib.request.Request(f"{ENA_URL}?{q}", headers={"User-Agent": "agentic-16s"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = resp.read().decode()
                break
            except Exception as e:  # noqa: BLE001 - logged and retried
                if attempt == 2:
                    common.log_event(pmid, "fetch ENA file report", "failure", f"{project}: {e}",
                                     source=ENA_URL, step="archive")
                    raise
                time.sleep(5 * (attempt + 1))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(body)
        n = max(0, len(body.strip().splitlines()) - 1)
        common.log_event(pmid, "fetch ENA file report", "success", f"{project}: {n} runs",
                         source=f"{ENA_URL}?accession={project}&result=read_run", files=[cache], step="archive")
    return {r["run_accession"]: r for r in common.read_tsv(cache)}


# ---------------------------------------------------------------- run source

def db_run_states(pmid: str) -> dict[str, str]:
    """core.run processing_state per run when the database is reachable (psql, DATABASE_URL); else {}.

    The database is the source of truth for which runs to process: it marks runs the authors did not
    use (skipped_not_used) and non-16S runs (skipped_not_16s)."""
    url = os.environ.get("DATABASE_URL") or common._read_env_file(common.REPO / ".env").get("DATABASE_URL")
    if not url or not shutil.which("psql"):
        return {}
    sql = ("select r.run_accession, r.processing_state from core.run r join core.sample s using (sample_id) "
           f"join core.study st using (study_id) where st.study_key like '%-{int(pmid)}'")
    proc = subprocess.run(["psql", url, "-Atc", sql, "-F", "\t"], capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"database not used for the run list: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return {}
    return dict(line.split("\t") for line in proc.stdout.splitlines() if "\t" in line)


def load_runs(pmid: str) -> list[dict]:
    """16S runs of a study with archive file info: the study's sample sheet, ENA's file report, and
    the database's exclusions (db_run_states) when the database is reachable."""
    sheet = [r for r in common.read_tsv(common.STUDIES / pmid / "sample_sheet.tsv") if r["is_16s"] == "yes"]
    states = db_run_states(pmid)
    skipped = {a for a, st in states.items() if st.startswith("skipped")}
    if skipped:
        print(f"database excludes {len(skipped)} run(s): {', '.join(sorted(skipped)[:5])}")
    sheet = [r for r in sheet if r["run_accession"] not in skipped]
    ena: dict[str, dict] = {}
    for project in sorted({r["bioproject"] for r in sheet}):
        ena.update(ena_filereport(pmid, project))
    runs = []
    for r in sheet:
        e = ena.get(r["run_accession"], {})
        runs.append({
            "run_accession": r["run_accession"],
            "biosample": r["biosample"],
            "bioproject": r["bioproject"],
            "library_strategy": e.get("library_strategy") or r["library_strategy"],
            "library_layout": e.get("library_layout", ""),
            "instrument_platform": e.get("instrument_platform", ""),
            "instrument_model": e.get("instrument_model", ""),
            "read_count": e.get("read_count", ""),
            "fastq_ftp": e.get("fastq_ftp", ""),
            "fastq_md5": e.get("fastq_md5", ""),
            "fastq_bytes": e.get("fastq_bytes", ""),
            "group": r["group"],
            "sample_type": r["sample_type"],
        })
    return runs


MATE_FILE = re.compile(r"_[12]\.fastq\.gz$")


def resolve_layout(run: dict, protocol: dict) -> tuple[str, str]:
    """The layout to process a run with, and why it differs from ENA's, if it does.

    ENA's library_layout is what the submitter typed. Some studies (36111238) declare SINGLE
    while ENA serves an R1/R2 pair (`_1`/`_2` files): followed blindly, the run would be processed
    from R1 alone. When the protocol (from the paper) says PAIRED and ENA serves exactly one mate
    pair, the run is processed as PAIRED and the manifest says so. Without the protocol's
    support it is a mismatch for a person to look at, never a silent guess.
    """
    ena = (run.get("library_layout") or "").upper()
    urls = [u for u in run.get("fastq_ftp", "").split(";") if u]
    mates = [u for u in urls if MATE_FILE.search(u)]
    if ena == "SINGLE" and len(mates) == 2:
        if (protocol.get("layout") or "").upper() == "PAIRED":
            return "PAIRED", "layout corrected: ENA says SINGLE but serves _1/_2 mate files; protocol.json says PAIRED"
        return ena, "ENA says SINGLE but serves _1/_2 mate files; protocol.json does not say PAIRED"
    return ena, ""


def pick_files(run: dict) -> list[str]:
    """The R1/R2 (or single) FASTQ URLs. ENA sometimes adds an orphan-reads file for PAIRED runs."""
    urls = [u for u in run["fastq_ftp"].split(";") if u]
    if run["library_layout"] == "PAIRED":
        pe = sorted(u for u in urls if re.search(r"_[12]\.fastq\.gz$", u))
        return pe if len(pe) == 2 else urls
    return urls


def assign_protocol(run: dict, protocols: list[dict]) -> str | None:
    for p in protocols:
        sel = p.get("select") or {}
        if all(run.get(k) == v for k, v in sel.items()):
            return p["label"]
    return None


def balanced_subset(runs: list[dict], n: int) -> list[dict]:
    """n runs spread across groups (case/control/other), deterministic."""
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(runs, key=lambda r: r["run_accession"]):
        by_group[r["group"] or "unknown"].append(r)
    order = sorted(by_group)
    out: list[dict] = []
    i = 0
    while len(out) < n and any(by_group.values()):
        g = order[i % len(order)]
        if by_group[g]:
            out.append(by_group[g].pop(0))
        i += 1
    return out


MANIFEST_FIELDS = ["run_accession", "protocol", "group", "sample_type", "library_strategy", "library_layout",
                   "instrument_platform", "instrument_model", "read_count", "n_files", "fastq_urls",
                   "fastq_md5", "fastq_bytes", "status", "status_reason", "library_layout_archive"]


def prepare(pmid: str, subset: int | None = None) -> dict[str, Path]:
    protocols = common.load_protocols(pmid)
    by_protocol = {p["label"]: p for p in protocols}
    runs = load_runs(pmid)
    by_label: dict[str, list[dict]] = defaultdict(list)
    unassigned = 0
    corrected: dict[str, int] = defaultdict(int)
    for r in runs:
        label = assign_protocol(r, protocols)
        if label is None:
            unassigned += 1
            continue
        layout, layout_note = resolve_layout(r, by_protocol[label])
        r["library_layout_archive"] = r["library_layout"]
        r["library_layout"] = layout
        if layout != r["library_layout_archive"]:
            corrected[label] += 1
        files = pick_files(r)
        r.update(protocol=label, n_files=len(files), fastq_urls=";".join(files), status="ok", status_reason=layout_note)
        if layout_note and layout == r["library_layout_archive"]:
            r.update(status="layout_mismatch", status_reason=layout_note)
        elif not files:
            r.update(status="no_fastq", status_reason="ENA lists no FASTQ for this run")
        elif r["library_layout"] == "PAIRED" and len(files) != 2:
            r.update(status="layout_mismatch", status_reason=f"PAIRED but {len(files)} FASTQ files")
        by_label[label].append(r)

    out: dict[str, Path] = {}
    for label, rs in by_label.items():
        if subset:
            rs = balanced_subset([r for r in rs if r["status"] == "ok"], subset)
        tag = common.run_tag(label, subset)
        d = common.protocol_dir(pmid, tag)
        common.write_tsv(d / "manifest.tsv", rs, MANIFEST_FIELDS)
        ok = [r["run_accession"] for r in rs if r["status"] == "ok"]
        (d / "runs.csv").write_text("\n".join(ok) + "\n")
        bad = [r for r in rs if r["status"] != "ok"]
        gb = sum(int(b) for r in rs for b in r["fastq_bytes"].split(";") if b) / 1e9
        common.log_event(
            pmid, "build run manifest", "success" if not bad else "partial",
            f"{tag}: {len(ok)} runs to fetch ({gb:.2f} GB){' (subset)' if subset else ''}; "
            f"{len(bad)} without usable FASTQ; {unassigned} 16S runs matched no protocol"
            + (f"; {corrected[label]} run(s) declared SINGLE in ENA processed as PAIRED (ENA serves _1/_2 mate "
               f"files and protocol.json says PAIRED)" if corrected[label] else ""),
            source="studies/<PMID>/sample_sheet.tsv + ENA filereport", files=[d / "manifest.tsv"])
        out[tag] = d
    return out


# ---------------------------------------------------------------- finalize

ILLUMINA_HEADER = re.compile(r"([A-Za-z0-9_-]+):(\d+):([A-Za-z0-9-]+):(\d+):\d+:\d+:\d+")


def sequencing_batch(fastq_gz: Path) -> str:
    """instrument_run_flowcell from the first Illumina read header, '1' if the header has none."""
    with gzip.open(fastq_gz, "rt") as fh:
        header = fh.readline().strip()
    return batch_from_header(header)


def batch_from_header(header: str) -> str:
    m = ILLUMINA_HEADER.search(header)
    if not m:
        return "1"
    instrument, run_no, flowcell, _lane = m.groups()
    return re.sub(r"[^A-Za-z0-9_]", "_", f"{instrument}_{run_no}_{flowcell}")


def finalize(pmid: str, tag: str, fetch_outdir: Path) -> Path:
    """Write the ampliseq samplesheet: sample = run accession, run = sequencing batch."""
    d = common.protocol_dir(pmid, tag)
    manifest = {r["run_accession"]: r for r in common.read_tsv(d / "manifest.tsv")}
    fetched: dict[str, dict] = {}
    ss = fetch_outdir / "samplesheet" / "samplesheet.csv"
    if ss.exists():
        with open(ss, newline="") as fh:
            for row in csv.DictReader(fh):
                fetched[row.get("run_accession", "")] = row
    rows, missing = [], []
    for acc, m in manifest.items():
        if m["status"] != "ok":
            continue
        row = fetched.get(acc)
        if row and row.get("fastq_1"):
            f1, f2 = row["fastq_1"], row.get("fastq_2", "")
        else:  # fall back to the files on disk
            hits = sorted((fetch_outdir / "fastq").glob(f"*{acc}*.fastq.gz"))
            if not hits:
                missing.append(acc)
                continue
            f1 = str(hits[0])
            f2 = str(hits[1]) if len(hits) > 1 else ""
        if m["library_layout"] == "PAIRED" and not f2:
            missing.append(acc)
            continue
        rows.append({"sample": acc, "fastq_1": f1, "fastq_2": f2 if m["library_layout"] == "PAIRED" else "",
                     "run": sequencing_batch(Path(f1))})
    fields = ["sample", "fastq_1", "fastq_2", "run"] if any(r["fastq_2"] for r in rows) else ["sample", "fastq_1", "run"]
    out = d / "samplesheet.tsv"
    common.write_tsv(out, rows, fields)
    batches = sorted({r["run"] for r in rows})
    common.log_event(
        pmid, "write ampliseq samplesheet", "success" if not missing else "partial",
        f"{tag}: {len(rows)} runs, {len(batches)} sequencing batch(es) from read headers "
        f"({', '.join(batches[:5])}); missing after download: {', '.join(missing) or 'none'}",
        source=str(ss), files=[out])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pmid")
    ap.add_argument("--subset", type=int)
    a = ap.parse_args()
    for k, v in prepare(a.pmid, a.subset).items():
        print(k, v)
