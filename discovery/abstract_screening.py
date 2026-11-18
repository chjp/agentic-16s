#!/usr/bin/env python3
"""
Abstract Screening Script
Uses an LLM (default: Claude Haiku through the Claude Code CLI) to screen paper
abstracts against user-defined inclusion/exclusion criteria.
"""

import argparse
import csv
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm_cli import call_llm

DECISIONS = ('INCLUDE', 'EXCLUDE', 'UNCERTAIN', 'ERROR')


def load_criteria(criteria_text=None, criteria_file=None):
    """
    Load screening criteria from text or file.

    Args:
        criteria_text: Inline criteria string
        criteria_file: Path to criteria file

    Returns:
        Criteria string
    """
    if criteria_file:
        with open(criteria_file, 'r', encoding='utf-8') as f:
            return f.read().strip()
    elif criteria_text:
        return criteria_text
    else:
        raise ValueError("Must provide either --criteria or --criteria-file")


def build_screening_prompt(title, abstract, criteria):
    """
    Build the prompt to screen one abstract.

    Args:
        title: Paper title
        abstract: Paper abstract
        criteria: Inclusion/exclusion criteria

    Returns:
        Prompt string
    """
    prompt = f"""You are screening scientific papers for a specific research project.

SCREENING CRITERIA:
{criteria}

PAPER TO EVALUATE:
Title: {title}

Abstract: {abstract}

TASK:
Analyze this paper and determine if it meets the screening criteria.

Respond with ONLY a JSON object in this exact format, with the fields in this order:
{{
  "reason": "1-2 sentences: check the paper against each criterion, naming the sample type sequenced",
  "decision": "INCLUDE" or "EXCLUDE" or "UNCERTAIN"
}}

Rules:
- Write the reason first, then give the decision that follows from it
- INCLUDE: Paper clearly meets ALL inclusion criteria
- EXCLUDE: Paper clearly violates at least one criterion
- UNCERTAIN: Insufficient information in abstract to make a clear decision
- Keep the reason concise and specific
- Output ONLY the JSON, nothing else"""

    return prompt


# Phrases that mean the model's own reason argues for exclusion. Small models
# sometimes commit to a decision that their reason then contradicts.
_EXCLUDE_SIGNS = re.compile(
    r"should be excluded|must be excluded|is excluded|leads to exclusion|"
    r"fails? (?:inclusion|exclusion) criteri|meets exclusion criteri|"
    r"violates (?:inclusion|exclusion)? ?criteri",
    re.IGNORECASE)


def contradicts_include(reason):
    """True if a reason given with INCLUDE argues for exclusion."""
    return bool(_EXCLUDE_SIGNS.search(reason or ''))


def screen_one(paper, criteria, model, backend, raw_dir, timeout):
    """Screen a single paper and add screening columns to it."""
    pmid = paper.get('pmid', '')
    title = paper.get('title', '')
    abstract = paper.get('abstract', '')

    if not abstract or abstract.strip() == '':
        paper['screening_decision'] = 'UNCERTAIN'
        paper['screening_reason'] = 'No abstract available'
        paper['screening_model'] = ''
        paper['screening_cost_usd'] = '0'
        return paper

    prompt = build_screening_prompt(title, abstract, criteria)
    data, meta = call_llm(prompt, stage='screening', key=pmid, model=model,
                          backend=backend, raw_dir=raw_dir, timeout=timeout)

    if data is None:
        decision, reason = 'ERROR', f"LLM call failed: {meta.get('error')}"
    else:
        decision = str(data.get('decision', 'ERROR')).upper().strip()
        reason = str(data.get('reason', 'No reason provided'))
        if decision not in DECISIONS:
            reason = f"Invalid decision from model: {decision}"
            decision = 'ERROR'
        elif decision == 'INCLUDE' and contradicts_include(reason):
            reason = f"[INCLUDE contradicted by its own reason, sent to review] {reason}"
            decision = 'UNCERTAIN'

    paper['screening_decision'] = decision
    paper['screening_reason'] = reason
    paper['screening_model'] = meta.get('model', '') if data is not None else ''
    paper['screening_cost_usd'] = str(meta.get('cost_usd') or 0)
    return paper


def screen_papers(input_file, criteria, model='haiku', backend='claude', workers=4,
                  raw_dir='llm_raw', timeout=180, resume_from=None):
    """
    Screen papers in parallel, keeping input order.

    Returns:
        List of paper dictionaries with screening results
    """
    with open(input_file, 'r', encoding='utf-8') as f:
        papers = list(csv.DictReader(f, delimiter='\t'))

    if resume_from:
        pmids = [p.get('pmid') for p in papers]
        if resume_from in pmids:
            papers = papers[pmids.index(resume_from):]
            print(f"Resuming from PMID {resume_from}", file=sys.stderr)

    total = len(papers)
    done = [0]

    def task(paper):
        result = screen_one(paper, criteria, model, backend, raw_dir, timeout)
        done[0] += 1
        print(f"  [{done[0]}/{total}] PMID {result.get('pmid')}: {result['screening_decision']}",
              file=sys.stderr)
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(task, papers))


def save_screened_papers(papers, output_file):
    """
    Save screened papers to TSV file.

    Args:
        papers: List of paper dictionaries
        output_file: Output TSV file path
    """
    rows_written = 0

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = None

        for paper in papers:
            if writer is None:
                fieldnames = list(paper.keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter='\t')
                writer.writeheader()

            writer.writerow(paper)
            rows_written += 1

    return rows_written


def main():
    parser = argparse.ArgumentParser(
        description='Screen paper abstracts with an LLM (default: Claude Haiku via Claude Code CLI)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  # IBD study, Haiku through the Claude Code CLI (default)
  python3 abstract_screening.py -i papers.tsv -o screened.tsv \\
    --criteria-file discovery/criteria/ibd_criteria.txt

  # Dental caries study with inline criteria
  python3 abstract_screening.py -i papers.tsv -o screened.tsv \\
    --criteria "Include: human saliva samples, dental caries, 16S sequencing. Exclude: plaque samples"

  # Use the Gemini CLI instead
  python3 abstract_screening.py -i papers.tsv -o screened.tsv --criteria-file criteria.txt \\
    --backend gemini -m gemini-2.5-flash
        """
    )

    parser.add_argument('-i', '--input', required=True,
                        help='Input TSV file from paper_discovery.py or sample_epmc.py')
    parser.add_argument('-o', '--output', required=True,
                        help='Output TSV file with screening results')

    criteria_group = parser.add_mutually_exclusive_group(required=True)
    criteria_group.add_argument('--criteria',
                                help='Inline screening criteria (inclusion/exclusion)')
    criteria_group.add_argument('--criteria-file',
                                help='File containing screening criteria')

    parser.add_argument('--backend', choices=['claude', 'gemini'], default='claude',
                        help='LLM CLI to call (default: claude)')
    parser.add_argument('-m', '--model', default='haiku',
                        help='Model name or alias (default: haiku)')
    parser.add_argument('--workers', type=int, default=4,
                        help='Abstracts screened in parallel (default: 4)')
    parser.add_argument('--timeout', type=int, default=180,
                        help='Seconds per LLM call (default: 180)')
    parser.add_argument('--raw-dir', default='llm_raw',
                        help='Where raw LLM replies are stored and reused (default: llm_raw)')
    parser.add_argument('--resume-from',
                        help='Resume from specific PMID (if interrupted)')

    args = parser.parse_args()
    if args.backend == 'gemini' and args.model == 'haiku':
        args.model = None

    print("Loading screening criteria...", file=sys.stderr)
    criteria = load_criteria(args.criteria, args.criteria_file)

    if not Path(args.input).exists():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    print(f"Screening papers from {args.input} with {args.backend}/{args.model} "
          f"({args.workers} workers)...", file=sys.stderr)
    t0 = time.time()
    screened = screen_papers(args.input, criteria, model=args.model, backend=args.backend,
                             workers=args.workers, raw_dir=args.raw_dir,
                             timeout=args.timeout, resume_from=args.resume_from)

    rows_written = save_screened_papers(screened, args.output)

    print(f"\nDone! Screened {rows_written} papers in {time.time() - t0:.0f}s", file=sys.stderr)
    print(f"Results saved to {args.output}", file=sys.stderr)

    decisions = [p['screening_decision'] for p in screened]
    cost = sum(float(p.get('screening_cost_usd') or 0) for p in screened)
    print("\nSummary:", file=sys.stderr)
    for d in DECISIONS:
        print(f"  {d}: {decisions.count(d)}", file=sys.stderr)
    print(f"  API-price equivalent reported by the CLI: ${cost:.4f} "
          f"(not billed on a claude.ai subscription login)", file=sys.stderr)


if __name__ == '__main__':
    main()
