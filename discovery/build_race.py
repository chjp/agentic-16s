#!/usr/bin/env python3
"""
Join the stage tables of one run by PMID, decide where each paper stopped,
and write the race-track page (race.html) from discovery/race_template.html.

Example:
  python3 discovery/build_race.py --run-dir discovery/runs/ibd100 --disease IBD --pool-size 1462 --seed 16
"""

import argparse
import collections
import csv
import json
import os
import sys
import time
from pathlib import Path

GATES = [
    {"id": "g1", "name": "Abstract screening", "note": "Haiku reads the title and abstract",
     "branches": ["Excluded", "Uncertain", "Error"]},
    {"id": "g2", "name": "Full text", "note": "PMC open-access XML only",
     "branches": ["No PMC copy", "Not open access", "Download failed"]},
    {"id": "g3", "name": "Supplements", "note": "Files listed in the XML, from Europe PMC",
     "branches": [], "pickup": True},
    {"id": "g4", "name": "Read by Haiku", "note": "Accessions checked against the paper text",
     "branches": ["No accession", "On request", "Other archive", "Extraction failed"]},
    {"id": "g5", "name": "SRA lookup", "note": "NCBI SRA runs and BioSamples",
     "branches": ["No runs in NCBI", "Lookup failed"]},
]


def load(path, key="pmid"):
    if not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {r[key]: r for r in csv.DictReader(f, delimiter="\t")}


def short(text, n=220):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def sample_file_stats(path):
    """Runs, BioSamples and library strategies in one per-paper SRA table."""
    if not path or not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    return {
        "runs": len(rows),
        "biosamples": len({r["biosample"] for r in rows if r.get("biosample")}),
        "strategies": dict(collections.Counter(r.get("library_strategy") or "?" for r in rows).most_common(3)),
        "instrument": collections.Counter(r.get("library_instrument") or "?" for r in rows).most_common(1)[0][0],
    }


def place(p, scr, ft, sup, ext, sra, run_dir):
    """Return (stop, branch, reason) and fill finisher details into p."""
    s = scr.get(p["pmid"])
    if not s:
        return "g1", "Error", "Not screened (row missing)"
    decision = s.get("screening_decision", "ERROR")
    if decision != "INCLUDE":
        branch = {"EXCLUDE": "Excluded", "UNCERTAIN": "Uncertain"}.get(decision, "Error")
        return "g1", branch, short(s.get("screening_reason"))
    p["screen_reason"] = short(s.get("screening_reason"), 160)

    f = ft.get(p["pmid"], {})
    status = f.get("download_status", "")
    if status != "SUCCESS":
        branch = {"NO_PMC": "No PMC copy", "NOT_OA": "Not open access"}.get(status, "Download failed")
        return "g2", branch, short(f.get("download_error") or "Full text not downloaded")

    u = sup.get(p["pmid"], {})
    p["suppl"] = int(u.get("supplementary_count") or 0)
    p["suppl_types"] = u.get("supplementary_types", "")

    e = ext.get(p["pmid"])
    if not e or e.get("extraction_status") == "FAILED":
        return "g4", "Extraction failed", short((e or {}).get("extraction_error") or "No extraction row")
    for k_out, k_in in [("sample_size", "sample_size"), ("n_cases", "n_cases"), ("n_controls", "n_controls"),
                        ("region", "variable_region"), ("platform", "sequencing_platform"),
                        ("sample_type", "sample_type_confirmed"), ("location", "location")]:
        p[k_out] = e.get(k_in, "")
    if not (e.get("bioproject_accessions") or e.get("sra_accessions")):
        other = " ".join(x for x in [e.get("other_data_locations"), e.get("other_archives_regex")] if x)
        low = other.lower()
        dropped = e.get("accessions_unverified")
        found = e.get("accessions_regex")
        if "request" in low:
            return "g4", "On request", short(f"No archive accession; the paper says: {other}")
        if any(k in low for k in ("genbank", "ega", "gsa", "cra0", "cnsa", "cnp0", "qiita", "mg-rast",
                                  "figshare", "zenodo", "dryad", "github")):
            return "g4", "Other archive", short(f"Not in SRA; data location given as: {other}")
        reason = "No SRA or BioProject accession in the paper"
        if dropped:
            reason += f"; Haiku named {dropped}, which is not in the text"
        elif found:
            reason += f"; the text does contain {found}, probably re-used data"
        if e.get("ambiguities"):
            reason += f". Note: {e['ambiguities']}"
        return "g4", "No accession", short(reason)
    p["accessions"] = ";".join(x for x in [e.get("bioproject_accessions"), e.get("sra_accessions")] if x)

    r = sra.get(p["pmid"])
    if not r:
        return "g5", "Lookup failed", "Not looked up (row missing)"
    st = r.get("sra_extraction_status")
    if st == "SUCCESS" and r.get("sra_sample_count"):
        stats = sample_file_stats(Path(run_dir) / "sra_samples" / r.get("sra_sample_file", ""))
        p.update(stats)
        return "finish", "Finished", f"{stats.get('runs', r['sra_sample_count'])} runs retrieved from NCBI SRA"
    if st in ("NO_RUNS", "SUCCESS"):
        return "g5", "No runs in NCBI", short(r.get("sra_extraction_error") or "NCBI SRA returned no runs")
    return "g5", "Lookup failed", short(r.get("sra_extraction_error") or st)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--template", default=str(Path(__file__).with_name("race_template.html")))
    ap.add_argument("-o", "--output", help="Default: <run-dir>/race.html")
    ap.add_argument("--disease", default="IBD")
    ap.add_argument("--pool-size", type=int, default=0, help="Records the sample was drawn from")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--model", default="Claude Haiku (Claude Code CLI)")
    ap.add_argument("--elapsed-min", type=float, default=None,
                    help="Wall-clock minutes of the run (default: from file times)")
    args = ap.parse_args()

    d = Path(args.run_dir)
    with open(d / "papers.tsv", encoding="utf-8") as f:
        base = list(csv.DictReader(f, delimiter="\t"))
    scr = load(d / "papers_screened.tsv")
    ft = load(d / "papers_fulltext.tsv")
    sup = load(d / "papers_supplementary.tsv")
    ext = load(d / "papers_demographics_sra.tsv")
    sra = load(d / "papers_sra_samples.tsv")

    papers = []
    for row in base:
        p = {"pmid": row["pmid"], "pmc": row.get("pmc_id", ""), "title": short(row.get("title"), 300),
             "journal": row.get("journal", ""), "year": row.get("pub_date", ""), "oa": row.get("is_open_access", "")}
        p["stop"], p["branch"], p["reason"] = place(p, scr, ft, sup, ext, sra, d)
        papers.append(p)

    cost = sum(float(r.get("screening_cost_usd") or 0) for r in scr.values())
    cost += sum(float(r.get("extraction_cost_usd") or 0) for r in ext.values())
    outputs = [d / n for n in ("papers_screened.tsv", "papers_fulltext.tsv", "papers_supplementary.tsv",
                               "papers_demographics_sra.tsv", "papers_sra_samples.tsv") if (d / n).exists()]
    elapsed = (max(os.path.getmtime(x) for x in outputs) - os.path.getmtime(d / "papers.tsv")) if outputs else 0
    if args.elapsed_min is not None:
        elapsed = args.elapsed_min * 60
    llm_calls = sum(1 for _ in (d / "llm_raw").rglob("*.json")) if (d / "llm_raw").exists() else 0
    models = set()
    for f in (d / "llm_raw").rglob("*.json") if (d / "llm_raw").exists() else []:
        models.update(json.loads(f.read_text(encoding="utf-8")).get("models_used") or [])
    if models and args.model.startswith("Claude Haiku"):
        args.model = f"Claude Haiku ({', '.join(sorted(models))})"

    data = {
        "meta": {"disease": args.disease, "n": len(papers), "pool": args.pool_size, "seed": args.seed,
                 "model": args.model, "cost_usd": round(cost, 4), "elapsed_min": round(elapsed / 60, 1),
                 "llm_calls": llm_calls, "date": time.strftime("%Y-%m-%d")},
        "gates": GATES,
        "papers": papers,
    }

    counts = collections.Counter((p["stop"], p["branch"]) for p in papers)
    assert sum(counts.values()) == len(papers)
    for (stop, branch), n in sorted(counts.items()):
        print(f"  {stop:7s} {branch:20s} {n}", file=sys.stderr)

    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = Path(args.template).read_text(encoding="utf-8").replace("__RACE_DATA__", payload)
    out = Path(args.output) if args.output else d / "race.html"
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out} ({len(papers)} papers, {sum(p['stop'] == 'finish' for p in papers)} finished)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
