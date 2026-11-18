#!/usr/bin/env python3
"""
Supplementary Material Download Script (Stage 3.5)
Downloads supplementary files for open-access papers from Europe PMC.

The PMC OA Web Service (oa.fcgi) that this script used before returns 404 since
2026. Now the file names come from the <supplementary-material> elements of the
full-text XML downloaded in Stage 3, and each file is fetched from Europe PMC:
  https://europepmc.org/api/fulltextRepo?pprId={PMCID}&type=FILE&fileName={name}&mimeType=...
(Europe PMC's whole-paper supplementaryFiles zip was too slow and mostly figures.)
Files are saved as-is (zips are not unpacked) into supplementary_files/PMC{id}/,
with names reduced to their last path part, so nothing is written outside that folder.
"""

import argparse
import csv
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

EPMC_FILE = ("https://europepmc.org/api/fulltextRepo?pprId={pmc_id}&type=FILE"
             "&fileName={name}&mimeType=application/octet-stream")
EPMC_ZIP = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmc_id}/supplementaryFiles"
USER_AGENT = "paper-discover2sra/supplementary_download.py"
MAX_FILE_BYTES = 300 * 1024 * 1024  # skip single files larger than this


def load_fulltext_papers(input_file):
    """
    Load papers from fulltext download TSV and keep those with a successful download.

    Args:
        input_file: Path to TSV from fulltext_download.py

    Returns:
        List of paper dictionaries with successful full-text downloads
    """
    papers = []
    skipped_count = {"no_pmc": 0, "failed_download": 0}

    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')

        for paper in reader:
            pmc_id = paper.get('pmc_id', '').strip()
            download_status = paper.get('download_status', '').upper()

            if not pmc_id:
                skipped_count["no_pmc"] += 1
                continue

            if download_status != 'SUCCESS':
                skipped_count["failed_download"] += 1
                continue

            papers.append(paper)

    print(f"Loaded {len(papers)} papers for supplementary download", file=sys.stderr)
    print(f"Skipped: {skipped_count['no_pmc']} (no PMC ID), "
          f"{skipped_count['failed_download']} (no full text)", file=sys.stderr)

    return papers


def safe_name(name):
    """Last path part of a zip entry, with unusual characters replaced."""
    base = Path(name.replace('\\', '/')).name
    base = re.sub(r'[^A-Za-z0-9._ -]', '_', base).strip(' .')
    return base or None


def list_supplement_files(xml_path):
    """File names of the paper's supplementary material, from its PMC XML."""
    try:
        text = Path(xml_path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    names = []
    for block in re.findall(r'<supplementary-material\b.*?</supplementary-material>', text, re.S):
        for href in re.findall(r'xlink:href="([^"]+)"', block):
            if not href.startswith(('http://', 'https://', 'ftp://')) and href not in names:
                names.append(href)
    return names


def fetch_file(pmc_id, file_name, target, deadline_s=180):
    """Download one supplementary file from Europe PMC into target. Returns error or None."""
    return fetch_url(EPMC_FILE.format(pmc_id=pmc_id, name=quote(file_name)), target, deadline_s)


def fetch_url(url, target, deadline_s=180, max_bytes=None):
    """Stream url into target with a size cap and an overall deadline. Returns error or None."""
    max_bytes = max_bytes or MAX_FILE_BYTES
    t0 = time.time()
    try:
        req = Request(url, headers={'User-Agent': USER_AGENT})
        with urlopen(req, timeout=90) as resp, open(target, 'wb') as out:
            size = 0
            while chunk := resp.read(1 << 20):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"file larger than {max_bytes // 1_000_000} MB")
                if time.time() - t0 > deadline_s:
                    raise TimeoutError(f"download took over {deadline_s}s")
                out.write(chunk)
        return None
    except HTTPError as e:
        err = f"HTTP {e.code}"
    except (URLError, TimeoutError, ValueError, OSError) as e:
        err = str(e)
    Path(target).unlink(missing_ok=True)
    return err


def download_supplements(pmc_id, xml_path, output_dir):
    """
    Download every supplementary file the paper's XML lists, one by one.

    Returns:
        (status, list of saved paths relative to output_dir, error message or None)
        status is SUCCESS, NONE_FOUND or FAILED.
    """
    names = list_supplement_files(xml_path)
    if not names:
        return 'NONE_FOUND', [], "The paper lists no supplementary files"

    paper_dir = Path(output_dir) / pmc_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    saved, errors = [], []
    for name in names:
        local = safe_name(name)
        if not local:
            continue
        err = fetch_file(pmc_id, name, paper_dir / local)
        if err:
            errors.append(f"{name}: {err}")
        else:
            saved.append(str((paper_dir / local).relative_to(output_dir)))

    # Europe PMC refuses some files one by one (PDFs answer HTTP 403), but its
    # whole-paper zip still carries them. It is slow, so only try it when needed.
    refused = [e.split(': ', 1)[0] for e in errors if 'HTTP 403' in e]
    if refused:
        got, zip_err = fetch_from_zip(pmc_id, refused, paper_dir)
        for name in got:
            saved.append(str((paper_dir / safe_name(name)).relative_to(output_dir)))
        errors = [e for e in errors if e.split(': ', 1)[0] not in got]
        if zip_err:
            errors.append(f"zip fallback: {zip_err}")

    if not saved:
        return 'FAILED', [], '; '.join(errors)[:500]
    return 'SUCCESS', saved, ('; '.join(errors)[:500] or None)


def fetch_from_zip(pmc_id, wanted, paper_dir, deadline_s=180):
    """Take the wanted files out of Europe PMC's supplementaryFiles zip. Returns (names got, error)."""
    want = {safe_name(n): n for n in wanted}
    tmp = Path(paper_dir) / f".{pmc_id}_suppl.zip"
    err = fetch_url(EPMC_ZIP.format(pmc_id=pmc_id), tmp, deadline_s, max_bytes=MAX_FILE_BYTES)
    if err:
        return [], err
    got = []
    try:
        with zipfile.ZipFile(tmp) as archive:
            for info in archive.infolist():
                name = safe_name(info.filename)
                if info.is_dir() or name not in want or info.file_size > MAX_FILE_BYTES:
                    continue
                with archive.open(info) as src, open(Path(paper_dir) / name, 'wb') as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                got.append(want[name])
    except zipfile.BadZipFile:
        return [], "not a zip"
    finally:
        tmp.unlink(missing_ok=True)
    missing = [n for n in wanted if n not in got]
    return got, (f"{len(missing)} file(s) not in the zip" if missing else None)


def set_columns(paper, status, files):
    paper['supplementary_count'] = len(files)
    paper['supplementary_files'] = ';'.join(files)
    paper['supplementary_types'] = ';'.join(sorted({Path(f).suffix.lower().lstrip('.') for f in files}))
    paper['supplementary_status'] = status


def process_papers(papers, output_dir, delay=0.34, resume=True):
    """
    Download supplementary materials for all papers.

    Yields:
        Paper dictionaries with supplementary download status
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    for i, paper in enumerate(papers, 1):
        pmc_id = paper.get('pmc_id', '')
        pmid = paper.get('pmid', '')

        print(f"Processing {i}/{len(papers)}: PMID {pmid}, {pmc_id}", file=sys.stderr)

        paper_dir = Path(output_dir) / pmc_id
        if resume and paper_dir.exists() and any(paper_dir.iterdir()):
            files = sorted(str(p.relative_to(output_dir)) for p in paper_dir.iterdir() if p.is_file())
            set_columns(paper, 'SUCCESS' if files else 'NONE_FOUND', files)
            paper['supplementary_error'] = ''
            print(f"  SKIPPED: already downloaded ({len(files)} files)", file=sys.stderr)
            yield paper
            continue

        status, files, error_msg = download_supplements(pmc_id, paper.get('fulltext_xml_path', ''),
                                                        output_dir)
        set_columns(paper, status, files)
        paper['supplementary_error'] = error_msg or ''
        print(f"  {status}: {len(files)} files{' - ' + error_msg if error_msg else ''}", file=sys.stderr)

        yield paper

        if i < len(papers):
            time.sleep(delay)


def save_results(papers, output_file):
    """
    Save papers with supplementary download status to TSV.

    Returns:
        Number of rows written
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
        description='Download supplementary materials from Europe PMC (Stage 3.5)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  python3 supplementary_download.py -i papers_fulltext.tsv -o papers_supplementary.tsv

  # Disable resume (re-download all)
  python3 supplementary_download.py -i papers_fulltext.tsv -o papers_supplementary.tsv --no-resume

Note:
  Files come from Europe PMC (open-access papers only). Statuses:
  SUCCESS, NONE_FOUND (paper has no supplementary files there), FAILED.
        """
    )

    parser.add_argument('-i', '--input', required=True,
                        help='Input TSV file from fulltext_download.py')
    parser.add_argument('-o', '--output', required=True,
                        help='Output TSV file with supplementary download status')
    parser.add_argument('--suppl-dir', default='supplementary_files',
                        help='Directory to save supplementary files (default: supplementary_files)')
    parser.add_argument('--delay', type=float, default=0.34,
                        help='Delay between downloads in seconds (default: 0.34)')
    parser.add_argument('--no-resume', action='store_true',
                        help='Re-download all files (ignore existing)')

    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading papers from {args.input}...", file=sys.stderr)
    papers = load_fulltext_papers(args.input)

    if not papers:
        print("No papers to process (no papers with successful full-text downloads)", file=sys.stderr)
        sys.exit(0)

    print(f"\nDownloading supplementary materials to {args.suppl_dir}/...\n", file=sys.stderr)
    processed = process_papers(papers, args.suppl_dir, delay=args.delay, resume=not args.no_resume)
    rows = list(processed)
    rows_written = save_results(rows, args.output)

    print(f"\nDone! Processed {rows_written} papers -> {args.output}", file=sys.stderr)
    statuses = [row['supplementary_status'] for row in rows]
    total_files = sum(int(row.get('supplementary_count') or 0) for row in rows)
    print("\nSummary:", file=sys.stderr)
    print(f"  SUCCESS: {statuses.count('SUCCESS')} papers ({total_files} files)", file=sys.stderr)
    print(f"  NONE_FOUND: {statuses.count('NONE_FOUND')} papers", file=sys.stderr)
    print(f"  FAILED: {statuses.count('FAILED')} papers", file=sys.stderr)


if __name__ == '__main__':
    main()
