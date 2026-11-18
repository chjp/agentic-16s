#!/usr/bin/env python3
"""
Full-text Download Script
Downloads full-text XML from PubMed Central for papers marked as INCLUDE in abstract screening
"""

import argparse
import csv
import sys
import time
from pathlib import Path
from Bio import Entrez


def load_screened_papers(input_file):
    """
    Load papers from abstract screening TSV and filter for INCLUDE with PMC ID.

    Args:
        input_file: Path to TSV from abstract_screening.py

    Returns:
        List of paper dictionaries that should be downloaded
    """
    papers = []
    skipped_count = {"no_pmc": 0, "not_included": 0}

    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')

        for paper in reader:
            decision = paper.get('screening_decision', '').upper()
            pmc_id = paper.get('pmc_id', '').strip()

            # Skip papers not marked as INCLUDE
            if decision != 'INCLUDE':
                skipped_count["not_included"] += 1
                continue

            # Papers without a PMC copy, or outside the open-access subset, are kept
            # in the output with a status but never fetched.
            paper['fulltext_xml_path'] = ''
            paper['download_error'] = ''
            if not pmc_id:
                skipped_count["no_pmc"] += 1
                paper['download_status'] = 'NO_PMC'
                paper['download_error'] = 'No PubMed Central copy'
            elif paper.get('is_open_access', 'Y').strip().upper() not in ('Y', 'YES', 'TRUE', '1'):
                skipped_count["not_oa"] = skipped_count.get("not_oa", 0) + 1
                paper['download_status'] = 'NOT_OA'
                paper['download_error'] = 'In PMC but not in the open-access subset'
            else:
                paper['download_status'] = ''

            papers.append(paper)

    print(f"Loaded {len(papers)} INCLUDE papers", file=sys.stderr)
    print(f"Skipped: {skipped_count['not_included']} (not INCLUDE); kept with status: "
          f"{skipped_count['no_pmc']} (no PMC ID), {skipped_count.get('not_oa', 0)} (not open access)",
          file=sys.stderr)

    return papers


def download_fulltext_xml(pmc_id, email, timeout=30):
    """
    Download full-text XML from PubMed Central.

    Args:
        pmc_id: PubMed Central ID (e.g., "PMC12075887")
        email: Email for NCBI API
        timeout: Request timeout in seconds

    Returns:
        Tuple of (success: bool, xml_content: str or None, error_msg: str or None)
    """
    Entrez.email = email

    try:
        # Remove "PMC" prefix if present for the API call
        pmc_number = pmc_id.replace('PMC', '')

        handle = Entrez.efetch(
            db="pmc",
            id=pmc_number,
            rettype="xml",
            retmode="xml"
        )
        xml_content = handle.read()
        handle.close()

        # Check if we got valid content
        if not xml_content or len(xml_content) < 100:
            return False, None, "Empty or invalid XML response"

        # Some publishers let PMC show the article but not hand out its XML:
        # the record then comes back without a <body>.
        probe = xml_content if isinstance(xml_content, bytes) else xml_content.encode('utf-8')
        if b'<body' not in probe:
            return False, None, "PMC returned no article body (publisher withholds the XML)"

        return True, xml_content, None

    except Exception as e:
        return False, None, str(e)


def save_xml_file(xml_content, pmc_id, output_dir):
    """
    Save XML content to file.

    Args:
        xml_content: XML content as bytes or string
        pmc_id: PubMed Central ID
        output_dir: Directory to save XML files

    Returns:
        Path to saved file
    """
    output_path = Path(output_dir) / f"{pmc_id}.xml"

    # Ensure content is bytes for writing
    if isinstance(xml_content, str):
        xml_content = xml_content.encode('utf-8')

    with open(output_path, 'wb') as f:
        f.write(xml_content)

    return str(output_path)


def process_papers(papers, email, output_dir, delay=0.34, resume=True):
    """
    Download full-text XML for all papers.

    Args:
        papers: List of paper dictionaries
        email: Email for NCBI API
        output_dir: Directory to save XML files
        delay: Delay between downloads (default: 0.34s = ~3 req/sec)
        resume: Skip already downloaded files

    Yields:
        Paper dictionaries with download status
    """
    # Create output directory if needed
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    for i, paper in enumerate(papers, 1):
        pmc_id = paper.get('pmc_id', '')
        pmid = paper.get('pmid', '')

        print(f"Processing {i}/{len(papers)}: PMID {pmid}, {pmc_id}", file=sys.stderr)

        # Already decided at load time (no PMC copy / not open access)
        if paper.get('download_status'):
            print(f"  {paper['download_status']}: {paper['download_error']}", file=sys.stderr)
            yield paper
            continue

        # Check if already downloaded (resume capability)
        expected_path = Path(output_dir) / f"{pmc_id}.xml"
        if resume and expected_path.exists():
            print("  SKIPPED: Already downloaded", file=sys.stderr)
            paper['fulltext_xml_path'] = str(expected_path)
            paper['download_status'] = 'SUCCESS'
            paper['download_error'] = ''
            yield paper
            continue

        # Download XML
        success, xml_content, error_msg = download_fulltext_xml(pmc_id, email)

        if success:
            # Save to file
            try:
                xml_path = save_xml_file(xml_content, pmc_id, output_dir)
                paper['fulltext_xml_path'] = xml_path
                paper['download_status'] = 'SUCCESS'
                paper['download_error'] = ''
                print(f"  SUCCESS: Saved to {xml_path}", file=sys.stderr)
            except Exception as e:
                paper['fulltext_xml_path'] = ''
                paper['download_status'] = 'FAILED'
                paper['download_error'] = f"File write error: {str(e)}"
                print(f"  FAILED: {str(e)}", file=sys.stderr)
        else:
            paper['fulltext_xml_path'] = ''
            paper['download_status'] = 'FAILED'
            paper['download_error'] = error_msg or 'Unknown error'
            print(f"  FAILED: {error_msg}", file=sys.stderr)

        yield paper

        # Rate limiting
        if i < len(papers):  # Don't delay after last paper
            time.sleep(delay)


def save_results(papers, output_file):
    """
    Save papers with download status to TSV.

    Args:
        papers: Iterator of paper dictionaries
        output_file: Output TSV file path

    Returns:
        Number of rows written
    """
    rows_written = 0

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = None

        for paper in papers:
            if writer is None:
                # Initialize writer with fieldnames from first paper
                fieldnames = list(paper.keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter='\t')
                writer.writeheader()

            writer.writerow(paper)
            rows_written += 1

            # Flush every 10 papers
            if rows_written % 10 == 0:
                f.flush()

    return rows_written


def main():
    parser = argparse.ArgumentParser(
        description='Download full-text XML from PubMed Central for screened papers',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  # Download full-text for all INCLUDE papers
  python3 fulltext_download.py -i papers_screened.tsv -o papers_fulltext.tsv

  # Custom output directory and email
  python3 fulltext_download.py -i papers_screened.tsv -o papers_fulltext.tsv \\
    --xml-dir fulltext_xml --email your.email@example.com

  # Disable resume (re-download all)
  python3 fulltext_download.py -i papers_screened.tsv -o papers_fulltext.tsv --no-resume
        """
    )

    parser.add_argument('-i', '--input', required=True,
                        help='Input TSV file from abstract_screening.py')
    parser.add_argument('-o', '--output', required=True,
                        help='Output TSV file with download status')
    parser.add_argument('--xml-dir', default='fulltext_xml',
                        help='Directory to save XML files (default: fulltext_xml)')
    parser.add_argument('-e', '--email', default='user@example.com',
                        help='Email address for NCBI (default: user@example.com)')
    parser.add_argument('--delay', type=float, default=0.34,
                        help='Delay between downloads in seconds (default: 0.34 = ~3 req/sec)')
    parser.add_argument('--no-resume', action='store_true',
                        help='Re-download all files (ignore existing)')

    args = parser.parse_args()

    # Check if input file exists
    if not Path(args.input).exists():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    # Load papers
    print(f"Loading papers from {args.input}...", file=sys.stderr)
    papers = load_screened_papers(args.input)

    if not papers:
        print("No papers to download (no INCLUDE papers with PMC IDs)", file=sys.stderr)
        sys.exit(0)

    # Download full-text
    print(f"\nDownloading full-text XML to {args.xml_dir}/...", file=sys.stderr)
    print(f"Rate limit: {1/args.delay:.1f} requests/second\n", file=sys.stderr)

    processed = process_papers(
        papers,
        args.email,
        args.xml_dir,
        delay=args.delay,
        resume=not args.no_resume
    )

    # Save results
    rows_written = save_results(processed, args.output)

    print(f"\nDone! Processed {rows_written} papers", file=sys.stderr)
    print(f"Results saved to {args.output}", file=sys.stderr)

    # Summary statistics
    with open(args.output, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        statuses = [row['download_status'] for row in reader]

    if statuses:
        print("\nSummary:", file=sys.stderr)
        for status in ('SUCCESS', 'FAILED', 'NO_PMC', 'NOT_OA'):
            print(f"  {status}: {statuses.count(status)}", file=sys.stderr)


if __name__ == '__main__':
    main()
