#!/usr/bin/env python3
"""
Turn a Europe PMC record file (JSON lines, gzip) into the papers.tsv that
abstract_screening.py expects, taking a random sample with a fixed seed.

Example:
  python3 discovery/sample_epmc.py \
    -i phase0/data/epmc_records_ibd.jsonl.gz -n 100 --seed 16 -o discovery/runs/ibd100/papers.tsv

  # a second batch that leaves out every paper of the first one
  python3 discovery/sample_epmc.py -i phase0/data/epmc_records_ibd.jsonl.gz -n 200 --seed 17 \
    --exclude discovery/runs/ibd100/papers.tsv -o discovery/runs/ibd200b/papers.tsv
"""

import argparse
import csv
import gzip
import html
import json
import random
import re
import sys

COLUMNS = ["pmid", "pubmed_url", "pmc_id", "doi", "title", "abstract", "authors",
           "journal", "pub_date", "is_open_access"]


def clean(text):
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-i", "--input", required=True, help="Europe PMC records (.jsonl.gz)")
    p.add_argument("-o", "--output", required=True, help="Output papers.tsv")
    p.add_argument("-n", type=int, default=100, help="Number of papers to sample (default 100)")
    p.add_argument("--seed", type=int, default=16, help="Random seed (default 16)")
    p.add_argument("--exclude", nargs="*", default=[], metavar="TSV",
                   help="papers.tsv files of earlier batches; their PMIDs are not sampled again")
    args = p.parse_args()

    seen = set()
    for path in args.exclude:
        with open(path, newline="", encoding="utf-8") as f:
            seen.update(row["pmid"] for row in csv.DictReader(f, delimiter="\t"))

    with gzip.open(args.input, "rt", encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    records = [r for r in records if r.get("pmid") and r.get("abstractText")]
    n_pool = len(records)
    records = [r for r in records if r["pmid"] not in seen]
    sample = random.Random(args.seed).sample(records, min(args.n, len(records)))

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter="\t")
        w.writeheader()
        for r in sample:
            pmcid = r.get("pmcid") or ""
            w.writerow({
                "pmid": r["pmid"],
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/",
                "pmc_id": pmcid if pmcid.startswith("PMC") else "",
                "doi": r.get("doi") if r.get("doi") not in (None, "None") else "",
                "title": clean(r.get("title")),
                "abstract": clean(r.get("abstractText")),
                "authors": "",
                "journal": clean(r.get("journal")),
                "pub_date": r.get("pubYear") or "",
                "is_open_access": r.get("isOpenAccess") or "N",
            })

    n_oa = sum(r.get("isOpenAccess") == "Y" for r in sample)
    n_pmc = sum(str(r.get("pmcid") or "").startswith("PMC") for r in sample)
    excluded = f", {n_pool - len(records)} excluded as already sampled" if seen else ""
    print(f"Sampled {len(sample)} of {len(records)} records{excluded} (seed {args.seed}): "
          f"{n_pmc} with a PMC ID, {n_oa} open access -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
