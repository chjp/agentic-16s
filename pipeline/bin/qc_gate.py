"""QC gate: summarise one processed study into studies/<PMID>/pipeline_qc.json.

verdict = pass, or review with reasons. A review verdict does not hide the data; it tells a
person what to look at before the study enters an analysis.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules; python3 -I drops this
import common  # noqa: E402

PRIMER_MIN = 80.0          # median % of reads where Cutadapt found the primers
MERGE_MIN = 0.70           # median merged / denoised pairs
IN_RANGE_MIN = 0.90        # share of reads in ASVs of plausible length for the amplicon
LEN_BELOW, LEN_ABOVE = 40, 20   # plausible ASV length window around the E. coli amplicon length
UNASSIGNED_PHYLUM_MAX = 0.10
FILTER_MEDIAN_MIN = 0.60   # median share of trimmed reads kept by DADA2 filterAndTrim
FILTER_RUN_MIN = 0.20      # any single run below this is called out
FAILED_RUNS_MAX = 0.10


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def gate(pmid: str, tag: str) -> dict:
    d = common.protocol_dir(pmid, tag)
    t = d / "tables"
    params = json.loads((d / "params.json").read_text())
    qc = common.read_tsv(t / "run_qc.tsv")
    asv = {a["sequence_md5"]: int(a["length"]) for a in common.read_tsv(t / "asv.tsv")}
    tax = {r["sequence_md5"]: r for r in common.read_tsv(t / "taxonomy_assignment.tsv")}
    ab = common.read_tsv(t / "abundance.tsv")
    integrity = json.loads((t / "integrity.json").read_text())
    groups = {r["run_accession"]: r["group"] for r in common.read_tsv(common.STUDIES / pmid / "sample_sheet.tsv")}

    reasons = []
    status = Counter(r["status"] for r in qc)
    n = len(qc)
    if n and status["failed"] / n > FAILED_RUNS_MAX:
        reasons.append(f"{status['failed']} of {n} runs failed")

    primer = [fnum(r["pct_primer_found"]) for r in qc if fnum(r["pct_primer_found"]) is not None]
    med_primer = round(statistics.median(primer), 1) if primer else None
    if med_primer is not None and not params["params"].get("skip_cutadapt") and med_primer < PRIMER_MIN:
        reasons.append(f"median primer-found {med_primer}% < {PRIMER_MIN}%")

    merge = [int(r["reads_merged"]) / int(r["reads_denoised"]) for r in qc
             if r["reads_merged"] and r["reads_denoised"] and int(r["reads_denoised"]) > 0]
    med_merge = round(statistics.median(merge), 3) if merge else None
    if med_merge is not None and med_merge < MERGE_MIN:
        reasons.append(f"median merged/denoised {med_merge} < {MERGE_MIN}: truncation, region or primers may be wrong")

    kept = {r["run_accession"]: int(r["reads_filtered"]) / int(r["reads_after_trim"]) for r in qc
            if r["reads_filtered"] and r["reads_after_trim"] and int(r["reads_after_trim"]) > 0}
    med_kept = round(statistics.median(kept.values()), 3) if kept else None
    if med_kept is not None and med_kept < FILTER_MEDIAN_MIN:
        reasons.append(f"median quality-filter retention {med_kept:.0%} < {FILTER_MEDIAN_MIN:.0%}: revisit truncation or maxEE")
    poor = sorted((v, r) for r, v in kept.items() if v < FILTER_RUN_MIN)
    if poor:
        reasons.append(f"{len(poor)} run(s) keep < {FILTER_RUN_MIN:.0%} of reads in quality filtering ("
                       + ", ".join(f"{r} {v:.0%}" for v, r in poor[:5]) + "): check their read quality, truncation or maxEE")

    retention = [int(r["reads_nonchim"]) / int(r["reads_input"]) for r in qc
                 if r["reads_nonchim"] and r["reads_input"] and int(r["reads_input"]) > 0]

    # read-weighted ASV length and taxonomy
    reads_by_len, reads_total, unassigned_phylum = Counter(), 0, 0
    genus_reads, genus_by_run, run_total = Counter(), defaultdict(Counter), Counter()
    for a in ab:
        c, m = int(a["count"]), a["sequence_md5"]
        reads_total += c
        reads_by_len[asv[m]] += c
        tx = tax.get(m, {})
        if not tx.get("phylum"):
            unassigned_phylum += c
        g = tx.get("genus") or "unassigned"
        genus_reads[g] += c
        genus_by_run[a["run_accession"]][g] += c
        run_total[a["run_accession"]] += c
    modal_len = reads_by_len.most_common(1)[0][0] if reads_by_len else None
    amplicon = params.get("observed", {}).get("expected_asv_length") or params.get("observed", {}).get("expected_amplicon_length")
    in_range = None
    if amplicon and reads_total:
        ok = sum(c for L, c in reads_by_len.items() if amplicon - LEN_BELOW <= L <= amplicon + LEN_ABOVE)
        in_range = round(ok / reads_total, 3)
        if in_range < IN_RANGE_MIN:
            reasons.append(f"only {in_range:.0%} of reads in ASVs of {amplicon - LEN_BELOW}-{amplicon + LEN_ABOVE} nt "
                           f"(expected amplicon ~{amplicon}); modal length {modal_len}")
    frac_unassigned = round(unassigned_phylum / reads_total, 3) if reads_total else None
    if frac_unassigned is not None and frac_unassigned > UNASSIGNED_PHYLUM_MAX:
        reasons.append(f"{frac_unassigned:.0%} of reads unassigned at phylum level (off-target or wrong orientation?)")
    if not integrity["pass"]:
        reasons.append("integrity checks failed: " + "; ".join(integrity["problems"]))

    # sanity check, not a result: mean relative abundance of Faecalibacterium by group
    faecali = defaultdict(list)
    for run, gc in genus_by_run.items():
        faecali[groups.get(run) or "unknown"].append(gc.get("Faecalibacterium", 0) / run_total[run])
    top = [{"genus": g, "share": round(c / reads_total, 3)} for g, c in genus_reads.most_common(8)] if reads_total else []

    out = {
        "pmid": pmid, "run_tag": tag, "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "pipeline": {"name": "nf-core/ampliseq", "version": common.versions().get("AMPLISEQ_VERSION"),
                     "nextflow": common.versions().get("NXF_VER"), "ref_db": params["params"].get("dada_ref_taxonomy")},
        "preflight": {k: params["observed"].get(k) for k in ("primer_source", "primers_in_reads", "orientation",
                                                               "pct_fwd_at_r1_start", "pct_rev_at_r2_start")},
        "non_default_params": {k: v for k, v in params["params"].items()
                               if k in ("sequencing_type", "skip_cutadapt", "trunclenf", "trunclenr", "dada_taxonomy_rc")},
        "runs": {"in_samplesheet": n, **dict(status)},
        "median_reads_nonchim": statistics.median([int(r["reads_nonchim"]) for r in qc if r["reads_nonchim"]] or [0]),
        "median_retention": round(statistics.median(retention), 3) if retention else None,
        "median_primer_found_pct": med_primer, "median_filter_retention": med_kept, "median_merge_rate": med_merge,
        "n_asv": len(asv), "modal_asv_length": modal_len, "expected_amplicon_length": amplicon,
        "share_reads_plausible_length": in_range, "share_reads_unassigned_phylum": frac_unassigned,
        "top_genera": top,
        "sanity_faecalibacterium_mean_share_by_group": {g: round(statistics.mean(v), 4) for g, v in faecali.items()},
        "verdict": "review" if reasons else "pass", "reasons": reasons,
    }
    name = "pipeline_qc.json" if "_subset" not in tag else f"pipeline_qc_{tag.split('_', 1)[1]}.json"
    path = common.STUDIES / pmid / name
    common.write_json(path, out)
    common.log_event(pmid, "QC gate", "success" if not reasons else "partial",
                     f"{tag}: verdict {out['verdict']}" + (f" ({'; '.join(reasons)})" if reasons else "")
                     + f"; {n} runs, {len(asv)} ASVs, median non-chimeric reads {out['median_reads_nonchim']}",
                     source=str(t), files=[path])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pmid")
    ap.add_argument("--tag", default="main")
    a = ap.parse_args()
    print(json.dumps(gate(a.pmid, a.tag), indent=2))
