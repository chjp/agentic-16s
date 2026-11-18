#!/usr/bin/env python3
"""
Demographic and SRA Extraction Script (Stage 5)
Extracts cohort information and SRA/BioProject accessions from full-text XML
with an LLM (default: Claude Haiku through the Claude Code CLI).

Every accession the model returns is checked against the paper text; any that
does not appear there word for word is dropped and listed in
`accessions_unverified`.
"""

import argparse
import csv
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm_cli import call_llm

MAX_CHARS = 400_000  # about 100k tokens, well inside Haiku's context window

# INSDC accessions that NCBI SRA can resolve
SRA_ACC = re.compile(r'\b((?:PRJ(?:NA|EB|DB)\d+)|(?:[SED]R[PXRS]\d{5,}))\b')
# Other archives that NCBI SRA cannot resolve (for diagnostics only)
OTHER_ACC = re.compile(r'\b((?:EGA[SDC]\d{8,})|(?:CRA\d{6,})|(?:CNP\d{7,})|(?:PRJCA\d{6,}))\b')


def load_papers_with_xml(input_file):
    """
    Load papers from fulltext TSV that have successful XML downloads.

    Returns:
        List of paper dictionaries with XML paths
    """
    papers = []
    skipped = {"no_xml": 0, "failed_download": 0}

    with open(input_file, 'r', encoding='utf-8') as f:
        for paper in csv.DictReader(f, delimiter='\t'):
            xml_path = paper.get('fulltext_xml_path', '').strip()
            if paper.get('download_status', '').upper() != 'SUCCESS' or not xml_path:
                skipped["failed_download"] += 1
                continue
            if not Path(xml_path).exists():
                skipped["no_xml"] += 1
                print(f"WARNING: XML file not found: {xml_path}", file=sys.stderr)
                continue
            papers.append(paper)

    print(f"Loaded {len(papers)} papers with XML files", file=sys.stderr)
    print(f"Skipped: {skipped['no_xml']} (file not found), "
          f"{skipped['failed_download']} (no full text)", file=sys.stderr)
    return papers


def read_xml_as_text(xml_file):
    """
    Return the paper's text without its reference list (saves tokens; accessions
    are in the methods, data-availability or back-matter sections, not references).
    """
    try:
        root = ET.parse(xml_file).getroot()
        for parent in root.iter():
            for child in list(parent):
                if child.tag == 'ref-list':
                    parent.remove(child)
        text = ' '.join(t.strip() for t in root.itertext() if t.strip())
        return re.sub(r'\s+', ' ', text)
    except ET.ParseError as e:
        print(f"WARNING: XML parse error for {xml_file}: {e}", file=sys.stderr)
        return Path(xml_file).read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        print(f"ERROR: Failed to read {xml_file}: {e}", file=sys.stderr)
        return None


def build_extraction_prompt(paper_text, title, topic):
    """
    Build the prompt to extract cohort data and archive accessions.
    """
    return f"""You are extracting cohort and sequencing metadata from a scientific paper about {topic} and the microbiome.

PAPER TITLE: {title}

FULL PAPER TEXT:
{paper_text}

TASK:
Extract the following information from anywhere in the paper.

1. Archive accessions for the sequencing data generated in THIS study:
   - NCBI/ENA/DDBJ: BioProject (PRJNA*, PRJEB*, PRJDB*) and SRA/ENA study, experiment or run (SRP*, ERP*, DRP*, SRX*, SRR*, ERR*)
   - Copy accessions exactly as written. Do not invent or complete partial accessions.
   - Leave out accessions of public datasets that the study only re-used from other work.
   - Common locations: Data Availability section, Methods, Acknowledgments
2. Other places the data is said to be (e.g. EGA, GSA/CRA, CNSA/CNP, Qiita, MG-RAST, "available on request")
3. Cohort: sample size, number of cases, number of controls, age, sex, location, health status, diet
4. Study metadata: sample type (stool, biopsy, saliva, ...), sequencing platform, 16S variable region

Respond with ONLY a JSON object in this exact format:
{{
  "sra_accessions": ["SRP420678"],
  "bioproject_accessions": ["PRJNA930569"],
  "other_data_locations": "EGA EGAS00001001234 (controlled access)",
  "sample_size": 90,
  "n_cases": 60,
  "n_controls": 30,
  "age_range": "18-65 years",
  "gender": "45 male, 45 female",
  "location": "China, Guangzhou",
  "health_status": "40 Crohn's disease, 20 ulcerative colitis, 30 healthy controls",
  "diet_info": null,
  "sample_type": "stool",
  "sequencing_platform": "Illumina MiSeq",
  "variable_region": "V3-V4",
  "extraction_confidence": "high",
  "ambiguities": null,
  "notes": null
}}

IMPORTANT RULES:
- Use JSON null for missing fields; use [] for no accessions
- sample_size = number of participants sequenced; n_cases / n_controls as the paper defines them (null if no control group)
- extraction_confidence: "high" (explicit in paper), "medium" (inferred), "low" (uncertain)
- ambiguities: multiple BioProjects (explain which is which), conflicting numbers, data "available on request", mixed sample types; null if none
- Output ONLY the JSON, nothing else"""


def as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r'[;,\s]+', value)
    return [str(v).strip() for v in value if str(v).strip()]


def text_or_blank(value):
    return '' if value in (None, '', [], 'null') else str(value)


def verify_accessions(candidates, paper_text):
    """Split accessions into those found verbatim in the text and those not found."""
    found, missing = [], []
    for acc in candidates:
        if re.search(rf'\b{re.escape(acc)}\b', paper_text):
            if acc not in found:
                found.append(acc)
        elif acc not in missing:
            missing.append(acc)
    return found, missing


def extract_from_paper(paper, topic, model, backend, raw_dir, timeout):
    """
    Extract cohort data and accessions from a single paper.
    """
    pmid = paper.get('pmid', '')
    title = paper.get('title', '')
    paper_text = read_xml_as_text(paper.get('fulltext_xml_path', ''))

    paper['extraction_cost_usd'] = '0'
    if not paper_text:
        paper['extraction_status'] = 'FAILED'
        paper['extraction_error'] = 'Failed to read XML file'
        return paper

    # Diagnostics: what a plain pattern search finds in the text
    paper['accessions_regex'] = ';'.join(dict.fromkeys(SRA_ACC.findall(paper_text)))
    paper['other_archives_regex'] = ';'.join(dict.fromkeys(OTHER_ACC.findall(paper_text)))

    if len(paper_text) > MAX_CHARS:
        print(f"  PMID {pmid}: {len(paper_text)} chars, truncating to {MAX_CHARS}", file=sys.stderr)
        paper_text = paper_text[:MAX_CHARS]

    prompt = build_extraction_prompt(paper_text, title, topic)
    data, meta = call_llm(prompt, stage='extraction', key=pmid, model=model,
                          backend=backend, raw_dir=raw_dir, timeout=timeout)
    paper['extraction_cost_usd'] = str(meta.get('cost_usd') or 0)

    if data is None:
        paper['extraction_status'] = 'FAILED'
        paper['extraction_error'] = f"LLM call failed: {meta.get('error')}"
        return paper

    sra, sra_bad = verify_accessions(as_list(data.get('sra_accessions')), paper_text)
    bp, bp_bad = verify_accessions(as_list(data.get('bioproject_accessions')), paper_text)
    # Older papers give NCBI BioProject IDs as bare numbers ("BioProject ID 82111");
    # those are PRJNA82111. Anything else that is not an accession is dropped.
    converted = []
    for acc in list(bp):
        if acc.isdigit() and re.search(r'BioProject', paper_text, re.I):
            bp[bp.index(acc)] = f"PRJNA{acc}"
            converted.append(acc)
    bp_bad += [a for a in bp if not SRA_ACC.fullmatch(a)]
    bp = [a for a in bp if SRA_ACC.fullmatch(a)]
    sra_bad += [a for a in sra if not SRA_ACC.fullmatch(a)]
    sra = [a for a in sra if SRA_ACC.fullmatch(a)]

    paper['sra_accessions'] = ';'.join(sra)
    paper['bioproject_accessions'] = ';'.join(bp)
    paper['accessions_unverified'] = ';'.join(sra_bad + bp_bad)
    paper['other_data_locations'] = text_or_blank(data.get('other_data_locations'))
    paper['sample_size'] = text_or_blank(data.get('sample_size'))
    paper['n_cases'] = text_or_blank(data.get('n_cases'))
    paper['n_controls'] = text_or_blank(data.get('n_controls'))
    paper['age_info'] = text_or_blank(data.get('age_range'))
    paper['gender_distribution'] = text_or_blank(data.get('gender'))
    paper['location'] = text_or_blank(data.get('location'))
    paper['health_status'] = text_or_blank(data.get('health_status'))
    paper['diet_info'] = text_or_blank(data.get('diet_info'))
    paper['sample_type_confirmed'] = text_or_blank(data.get('sample_type'))
    paper['sequencing_platform'] = text_or_blank(data.get('sequencing_platform'))
    paper['variable_region'] = text_or_blank(data.get('variable_region'))
    paper['extraction_confidence'] = str(data.get('extraction_confidence') or 'low').upper()
    paper['ambiguities'] = text_or_blank(data.get('ambiguities'))
    paper['extraction_notes'] = text_or_blank(data.get('notes'))
    if converted:
        paper['extraction_notes'] = (paper['extraction_notes'] + ' ' if paper['extraction_notes'] else '') + \
            f"Numeric BioProject IDs {', '.join(converted)} read as PRJNA accessions."
    paper['extraction_model'] = meta.get('model', '')

    has_acc = bool(sra or bp)
    has_cohort = bool(paper['sample_size'] or paper['n_cases'] or paper['age_info'])
    if has_acc and has_cohort:
        paper['extraction_status'] = 'SUCCESS'
    elif has_acc or has_cohort:
        paper['extraction_status'] = 'PARTIAL'
    else:
        paper['extraction_status'] = 'NO_DATA'
    paper['extraction_error'] = ''
    return paper


COLUMNS_ADDED = [
    'accessions_regex', 'other_archives_regex', 'sra_accessions', 'bioproject_accessions',
    'accessions_unverified', 'other_data_locations', 'sample_size', 'n_cases', 'n_controls',
    'age_info', 'gender_distribution', 'location', 'health_status', 'diet_info',
    'sample_type_confirmed', 'sequencing_platform', 'variable_region', 'extraction_confidence',
    'ambiguities', 'extraction_notes', 'extraction_model', 'extraction_cost_usd',
    'extraction_status', 'extraction_error',
]


def save_results(papers, output_file):
    """Save papers with extraction results to TSV."""
    if not papers:
        return 0
    fieldnames = list(papers[0].keys())
    fieldnames += [c for c in COLUMNS_ADDED if c not in fieldnames]
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter='\t', restval='')
        writer.writeheader()
        writer.writerows(papers)
    return len(papers)


def main():
    parser = argparse.ArgumentParser(
        description='Extract cohort data and SRA accessions from full-text XML (Stage 5)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  python3 demographic_sra_extraction.py -i papers_fulltext.tsv -o papers_demographics_sra.tsv \\
    --topic "inflammatory bowel disease"

  # Use the Gemini CLI instead
  python3 demographic_sra_extraction.py -i papers_fulltext.tsv -o out.tsv --backend gemini
        """
    )

    parser.add_argument('-i', '--input', required=True,
                        help='Input TSV file from fulltext_download.py')
    parser.add_argument('-o', '--output', required=True,
                        help='Output TSV file with extraction results')
    parser.add_argument('--topic', default='the disease under study',
                        help='Disease or topic named in the prompt (e.g. "inflammatory bowel disease")')
    parser.add_argument('--backend', choices=['claude', 'gemini'], default='claude',
                        help='LLM CLI to call (default: claude)')
    parser.add_argument('-m', '--model', default='haiku',
                        help='Model name or alias (default: haiku)')
    parser.add_argument('--workers', type=int, default=3,
                        help='Papers read in parallel (default: 3)')
    parser.add_argument('--timeout', type=int, default=300,
                        help='Seconds per LLM call (default: 300)')
    parser.add_argument('--raw-dir', default='llm_raw',
                        help='Where raw LLM replies are stored and reused (default: llm_raw)')

    args = parser.parse_args()
    if args.backend == 'gemini' and args.model == 'haiku':
        args.model = None

    if not Path(args.input).exists():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    papers = load_papers_with_xml(args.input)
    if not papers:
        print("No papers with XML files to process", file=sys.stderr)
        sys.exit(0)

    print(f"\nExtracting with {args.backend}/{args.model} ({args.workers} workers)...", file=sys.stderr)
    t0, total, done = time.time(), len(papers), [0]

    def task(paper):
        result = extract_from_paper(paper, args.topic, args.model, args.backend,
                                    args.raw_dir, args.timeout)
        done[0] += 1
        acc = result.get('bioproject_accessions') or result.get('sra_accessions') or '-'
        print(f"  [{done[0]}/{total}] PMID {result.get('pmid')}: "
              f"{result.get('extraction_status')} {acc}", file=sys.stderr)
        return result

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(task, papers))

    rows_written = save_results(results, args.output)
    print(f"\nDone! Processed {rows_written} papers in {time.time() - t0:.0f}s -> {args.output}",
          file=sys.stderr)

    statuses = [r.get('extraction_status', 'UNKNOWN') for r in results]
    with_acc = sum(1 for r in results if r.get('sra_accessions') or r.get('bioproject_accessions'))
    dropped = sum(1 for r in results if r.get('accessions_unverified'))
    cost = sum(float(r.get('extraction_cost_usd') or 0) for r in results)
    print("\nSummary:", file=sys.stderr)
    for s in ('SUCCESS', 'PARTIAL', 'NO_DATA', 'FAILED'):
        print(f"  {s}: {statuses.count(s)} papers", file=sys.stderr)
    print(f"  Papers with a verified SRA/BioProject accession: {with_acc}", file=sys.stderr)
    print(f"  Papers where an accession was dropped (not in text): {dropped}", file=sys.stderr)
    print(f"  Reported LLM cost: ${cost:.4f}", file=sys.stderr)


if __name__ == '__main__':
    main()
