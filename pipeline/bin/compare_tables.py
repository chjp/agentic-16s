#!/usr/bin/env python3
"""Compare two runs of one study (for example the GCE VM and Google Batch) table by table.

    python3 -I pipeline/bin/compare_tables.py REF_DIR NEW_DIR [--out compare.json]

REF_DIR and NEW_DIR are protocol directories (DATA_DIR/pipeline/<PMID>/<tag> or a copy of one) with
tables/ and params.json. Reported:
  params       every ampliseq parameter whose value differs (a difference here explains the rest)
  run_qc       per run, every read-count column (input -> non-chimeric) and the status
  asv          sequence md5 sets
  abundance    every (run, ASV) count
  taxonomy     per rank, ASVs whose label differs, among ASVs present in both; species only when
               both runs assigned species (skip_dada_addspecies leaves it to assignTaxonomy)
Exit 0 when counts, ASVs and abundance are identical, 1 otherwise. Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

RANKS = ["domain", "phylum", "class", "order_name", "family", "genus", "species"]


def read_tsv(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def param_values(d: Path) -> dict:
    p = d / "params.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    raw = raw.get("params", raw)
    return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in raw.items()}


def diff_params(ref: dict, new: dict) -> dict:
    return {k: {"ref": ref.get(k), "new": new.get(k)} for k in sorted(set(ref) | set(new)) if ref.get(k) != new.get(k)}


def compare_run_qc(ref: list[dict], new: list[dict]) -> dict:
    r = {x["run_accession"]: x for x in ref}
    n = {x["run_accession"]: x for x in new}
    cols = [c for c in (ref[0].keys() if ref else []) if c.startswith("reads_") or c == "status"]
    differ = []
    for acc in sorted(set(r) & set(n)):
        bad = {c: [r[acc].get(c), n[acc].get(c)] for c in cols if r[acc].get(c) != n[acc].get(c)}
        if bad:
            differ.append({"run": acc, **bad})
    return {"runs_ref": len(r), "runs_new": len(n), "only_ref": sorted(set(r) - set(n)),
            "only_new": sorted(set(n) - set(r)), "runs_differ": len(differ), "examples": differ[:10],
            "identical": not differ and set(r) == set(n)}


def compare_asv(ref: list[dict], new: list[dict]) -> dict:
    r, n = {x["sequence_md5"] for x in ref}, {x["sequence_md5"] for x in new}
    return {"asv_ref": len(r), "asv_new": len(n), "only_ref": len(r - n), "only_new": len(n - r), "identical": r == n}


def compare_abundance(ref: list[dict], new: list[dict]) -> dict:
    key = lambda x: (x["run_accession"], x["sequence_md5"])  # noqa: E731
    r = {key(x): int(x["count"]) for x in ref}
    n = {key(x): int(x["count"]) for x in new}
    differ = [k for k in set(r) | set(n) if r.get(k) != n.get(k)]
    return {"rows_ref": len(r), "rows_new": len(n), "cells_differ": len(differ),
            "reads_ref": sum(r.values()), "reads_new": sum(n.values()), "identical": not differ}


def compare_taxonomy(ref: list[dict], new: list[dict]) -> dict:
    r = {x["sequence_md5"]: x for x in ref}
    n = {x["sequence_md5"]: x for x in new}
    both = sorted(set(r) & set(n))
    out = {"asv_compared": len(both), "label_differs": {}}
    for rank in RANKS:
        if rank == "species" and not (any(r[k].get(rank) for k in both) and any(n[k].get(rank) for k in both)):
            out["label_differs"][rank] = "not compared (one run has no species)"
            continue
        diffs = [k for k in both if (r[k].get(rank) or "") != (n[k].get(rank) or "")]
        out["label_differs"][rank] = len(diffs)
        if rank == "genus":
            out["genus_examples"] = [{"sequence_md5": k, "ref": r[k].get(rank), "new": n[k].get(rank)} for k in diffs[:10]]
    return out


def compare(ref_dir: Path, new_dir: Path) -> dict:
    t_ref, t_new = ref_dir / "tables", new_dir / "tables"
    res = {
        "ref": str(ref_dir), "new": str(new_dir),
        "params_differ": diff_params(param_values(ref_dir), param_values(new_dir)),
        "run_qc": compare_run_qc(read_tsv(t_ref / "run_qc.tsv"), read_tsv(t_new / "run_qc.tsv")),
        "asv": compare_asv(read_tsv(t_ref / "asv.tsv"), read_tsv(t_new / "asv.tsv")),
        "abundance": compare_abundance(read_tsv(t_ref / "abundance.tsv"), read_tsv(t_new / "abundance.tsv")),
        "taxonomy": compare_taxonomy(read_tsv(t_ref / "taxonomy_assignment.tsv"),
                                     read_tsv(t_new / "taxonomy_assignment.tsv")),
    }
    res["counts_identical"] = all(res[k]["identical"] for k in ("run_qc", "asv", "abundance"))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref_dir", type=Path)
    ap.add_argument("new_dir", type=Path)
    ap.add_argument("--out", type=Path, help="also write the result as JSON here")
    a = ap.parse_args()
    res = compare(a.ref_dir, a.new_dir)
    text = json.dumps(res, indent=2, ensure_ascii=False)
    if a.out:
        a.out.write_text(text + "\n")
    print(text)
    return 0 if res["counts_identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
