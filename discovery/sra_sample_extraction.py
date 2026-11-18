#!/usr/bin/env python3
"""
Stage 6: SRA Sample Metadata Extraction from BioProject

For papers with unique BioProject accessions, retrieve detailed SRA sample metadata:
- SRA experiment/run accessions (SRX*, SRR*)
- Library information (instrument, strategy, source, selection, layout)
- BioSample accessions and attributes (host, age, sex, location, etc.)

Output: One TSV per paper with 1 row per sample in sra_samples/ directory.
"""

import argparse
import csv
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from Bio import Entrez


def parse_sra_xml(sra_xml: str) -> List[Dict]:
    """
    Parse SRA XML to extract experiment, library, and BioSample info.

    Returns a list of dicts (one per RUN), each containing:
    - srx: SRA experiment accession
    - srr: SRA run accession
    - library_name, library_instrument, library_strategy, library_source,
      library_selection, library_layout
    - biosample: BioSample accession
    """
    results = []

    try:
        root = ET.fromstring(sra_xml)

        # Find all EXPERIMENT_PACKAGE elements
        for exp_package in root.findall('.//EXPERIMENT_PACKAGE'):
            # Extract experiment info
            experiment = exp_package.find('.//EXPERIMENT')
            if experiment is None:
                continue

            srx = experiment.get('accession', '')

            # Extract library info
            lib_descriptor = exp_package.find('.//LIBRARY_DESCRIPTOR')
            library_name = ''
            library_strategy = ''
            library_source = ''
            library_selection = ''
            library_layout = ''

            if lib_descriptor is not None:
                lib_name_elem = lib_descriptor.find('LIBRARY_NAME')
                if lib_name_elem is not None:
                    library_name = lib_name_elem.text or ''

                lib_strategy_elem = lib_descriptor.find('LIBRARY_STRATEGY')
                if lib_strategy_elem is not None:
                    library_strategy = lib_strategy_elem.text or ''

                lib_source_elem = lib_descriptor.find('LIBRARY_SOURCE')
                if lib_source_elem is not None:
                    library_source = lib_source_elem.text or ''

                lib_selection_elem = lib_descriptor.find('LIBRARY_SELECTION')
                if lib_selection_elem is not None:
                    library_selection = lib_selection_elem.text or ''

                # Layout can be PAIRED or SINGLE
                layout_elem = lib_descriptor.find('LIBRARY_LAYOUT')
                if layout_elem is not None:
                    if layout_elem.find('PAIRED') is not None:
                        library_layout = 'PAIRED'
                    elif layout_elem.find('SINGLE') is not None:
                        library_layout = 'SINGLE'

            # Extract instrument info
            library_instrument = ''
            platform = exp_package.find('.//PLATFORM')
            if platform is not None:
                for platform_type in ['ILLUMINA', 'PACBIO_SMRT', 'ION_TORRENT', 'ONT_MINION', 'OXFORD_NANOPORE']:
                    instrument_elem = platform.find(f'.//{platform_type}/INSTRUMENT_MODEL')
                    if instrument_elem is not None:
                        library_instrument = instrument_elem.text or ''
                        break

            # Extract BioSample accession
            biosample = ''
            sample = exp_package.find('.//SAMPLE')
            if sample is not None:
                external_id = sample.find('.//EXTERNAL_ID[@namespace="BioSample"]')
                if external_id is not None:
                    biosample = external_id.text or ''

            # Extract all runs for this experiment
            run_set = exp_package.find('.//RUN_SET')
            if run_set is not None:
                for run in run_set.findall('RUN'):
                    srr = run.get('accession', '')

                    results.append({
                        'srx': srx,
                        'srr': srr,
                        'library_name': library_name,
                        'library_instrument': library_instrument,
                        'library_strategy': library_strategy,
                        'library_source': library_source,
                        'library_selection': library_selection,
                        'library_layout': library_layout,
                        'biosample': biosample
                    })
            else:
                # No runs found, still record experiment info
                results.append({
                    'srx': srx,
                    'srr': '',
                    'library_name': library_name,
                    'library_instrument': library_instrument,
                    'library_strategy': library_strategy,
                    'library_source': library_source,
                    'library_selection': library_selection,
                    'library_layout': library_layout,
                    'biosample': biosample
                })

    except ET.ParseError as e:
        print(f"ERROR: Failed to parse SRA XML: {e}", file=sys.stderr)

    return results


def parse_biosample_xml(biosample_xml: str) -> Dict:
    """
    Parse BioSample XML to extract attributes.

    Returns a dict with standard attributes and 'other_attributes' JSON.
    """
    attributes = {
        'host': '',
        'age': '',
        'sex': '',
        'location': '',
        'strain': '',
        'isolate': '',
        'isolation_source': '',
        'collection_date': '',
        'disease_status': '',
        'sample_type': '',
        'other_attributes': '{}'
    }

    other_attrs = {}

    try:
        root = ET.fromstring(biosample_xml)

        # Find all Attribute elements
        for attr in root.findall('.//Attribute'):
            attr_name = attr.get('attribute_name', '').lower()
            attr_value = attr.text or ''

            # Map to standard fields
            if attr_name == 'host':
                attributes['host'] = attr_value
            elif attr_name in ['age', 'host age']:
                attributes['age'] = attr_value
            elif attr_name in ['sex', 'gender']:
                attributes['sex'] = attr_value
            elif attr_name in ['geo_loc_name', 'geographic location', 'location']:
                attributes['location'] = attr_value
            elif attr_name == 'strain':
                attributes['strain'] = attr_value
            elif attr_name == 'isolate':
                attributes['isolate'] = attr_value
            elif attr_name in ['isolation_source', 'isolation source']:
                attributes['isolation_source'] = attr_value
            elif attr_name in ['collection_date', 'collection date']:
                attributes['collection_date'] = attr_value
            elif attr_name in ['disease', 'disease status', 'disease state']:
                attributes['disease_status'] = attr_value
            elif attr_name in ['sample_type', 'sample type', 'tissue']:
                attributes['sample_type'] = attr_value
            else:
                # Store in other_attributes
                other_attrs[attr_name] = attr_value

        # Convert other_attributes to JSON
        if other_attrs:
            attributes['other_attributes'] = json.dumps(other_attrs)

    except ET.ParseError as e:
        print(f"ERROR: Failed to parse BioSample XML: {e}", file=sys.stderr)

    return attributes


MAX_EXPERIMENTS = 5000
ENA_FILEREPORT = ("https://www.ebi.ac.uk/ena/portal/api/filereport?accession={acc}"
                  "&result=read_run&fields=run_accession&format=tsv")


def ena_run_count(accession: str) -> Optional[int]:
    """Number of public runs ENA lists for an accession, or None if ENA could not be asked."""
    from urllib.request import Request, urlopen
    try:
        req = Request(ENA_FILEREPORT.format(acc=accession), headers={'User-Agent': 'paper-discover2sra'})
        with urlopen(req, timeout=60) as resp:
            lines = resp.read().decode('utf-8', 'replace').strip().splitlines()
        return max(0, len(lines) - 1)
    except Exception:
        return None


def get_sra_experiments_for_bioproject(bioproject: str, email: str, delay: float) -> Tuple[bool, List[Dict], Optional[str]]:
    """
    Query NCBI Entrez to get all SRA experiments for a BioProject, or for an
    SRA/ENA study, experiment or run accession (SRP/ERP/DRP/SRX/SRR/ERR...).

    Returns:
        (success, list of experiment dicts, error_message)
    """
    Entrez.email = email

    try:
        term = f"{bioproject}[BioProject]" if bioproject.startswith('PRJ') else f"{bioproject}[Accession]"
        print(f"  Searching SRA for {term}...", file=sys.stderr)
        search_handle = Entrez.esearch(db="sra", term=term, retmax=MAX_EXPERIMENTS)
        search_results = Entrez.read(search_handle)
        search_handle.close()

        sra_ids = search_results.get('IdList', [])
        total_count = int(search_results.get('Count', len(sra_ids)))
        if total_count > len(sra_ids):
            print(f"  WARNING: {bioproject} has {total_count} experiments; only the first "
                  f"{len(sra_ids)} are fetched", file=sys.stderr)

        if not sra_ids:
            return True, [], None  # No experiments found (not an error)

        print(f"  Found {len(sra_ids)} SRA experiments", file=sys.stderr)
        time.sleep(delay)

        # Fetch SRA metadata in batches
        all_experiments = []
        batch_size = 200

        for i in range(0, len(sra_ids), batch_size):
            batch_ids = sra_ids[i:i+batch_size]
            print(f"  Fetching SRA metadata for {len(batch_ids)} experiments...", file=sys.stderr)

            fetch_handle = Entrez.efetch(db="sra", id=batch_ids, retmode="xml")
            sra_xml = fetch_handle.read()
            fetch_handle.close()

            # Parse SRA XML
            experiments = parse_sra_xml(sra_xml)
            all_experiments.extend(experiments)

            time.sleep(delay)

        return True, all_experiments, None

    except Exception as e:
        error_msg = f"Failed to fetch SRA experiments: {str(e)}"
        print(f"ERROR: {error_msg}", file=sys.stderr)
        return False, [], error_msg


def get_biosample_metadata(biosample_ids: List[str], email: str, delay: float) -> Dict[str, Dict]:
    """
    Query NCBI Entrez to get BioSample metadata for a list of BioSample IDs.

    Returns:
        Dict mapping biosample_id -> attributes dict
    """
    Entrez.email = email
    biosample_metadata = {}

    if not biosample_ids:
        return biosample_metadata

    try:
        # Fetch BioSample metadata in batches
        batch_size = 200

        for i in range(0, len(biosample_ids), batch_size):
            batch_ids = biosample_ids[i:i+batch_size]
            print(f"  Fetching BioSample metadata for {len(batch_ids)} samples...", file=sys.stderr)

            fetch_handle = Entrez.efetch(db="biosample", id=batch_ids, retmode="xml")
            biosample_xml = fetch_handle.read()
            fetch_handle.close()

            # Parse BioSample XML
            try:
                root = ET.fromstring(biosample_xml)

                for biosample_elem in root.findall('.//BioSample'):
                    accession = biosample_elem.get('accession', '')
                    if accession:
                        # Convert entire BioSample element to string for parsing
                        biosample_str = ET.tostring(biosample_elem, encoding='unicode')
                        attributes = parse_biosample_xml(biosample_str)
                        biosample_metadata[accession] = attributes

            except ET.ParseError as e:
                print(f"ERROR: Failed to parse BioSample XML: {e}", file=sys.stderr)

            time.sleep(delay)

    except Exception as e:
        print(f"ERROR: Failed to fetch BioSample metadata: {str(e)}", file=sys.stderr)

    return biosample_metadata


def extract_sra_samples_for_paper(
    row: Dict,
    output_dir: str,
    email: str,
    delay: float,
    resume: bool
) -> Tuple[int, str, str, Optional[str]]:
    """
    Extract SRA samples for a single paper.

    Returns:
        (sample_count, output_file_path, status, error_message)
    """
    pmid = row['pmid']
    pmc_id = row.get('pmc_id', '')
    bioproject = row.get('bioproject_accessions', '').strip()

    # Determine output filename (prefer PMC ID)
    if pmc_id and pmc_id.startswith('PMC'):
        output_filename = f"{pmc_id}_samples.tsv"
    else:
        output_filename = f"PMID{pmid}_samples.tsv"

    output_path = os.path.join(output_dir, output_filename)

    # Check resume
    if resume and os.path.exists(output_path):
        # Count existing samples
        try:
            with open(output_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter='\t')
                sample_count = sum(1 for _ in reader)
            print(f"  Skipping {pmid} (already processed, {sample_count} samples)", file=sys.stderr)
            return sample_count, output_filename, 'SUCCESS', None
        except Exception:
            pass  # File exists but couldn't read, reprocess

    # Which accessions to look up: every BioProject the paper names; without one,
    # fall back to SRA/ENA study accessions, then to experiment/run accessions.
    bioprojects = [a for a in bioproject.split(';') if a.strip()]
    sra_accs = [a for a in row.get('sra_accessions', '').split(';') if a.strip()]
    studies = [a for a in sra_accs if a[:3] in ('SRP', 'ERP', 'DRP')]
    others = [a for a in sra_accs if a not in studies][:50]
    lookups = bioprojects or studies or others

    if not lookups:
        return 0, '', 'SKIPPED', 'No BioProject or SRA accession'

    print(f"\nProcessing {pmid} ({';'.join(lookups)})...", file=sys.stderr)

    experiments, errors, seen_runs = [], [], set()
    for acc in lookups:
        success, found, error_msg = get_sra_experiments_for_bioproject(acc, email, delay)
        if not success:
            errors.append(f"{acc}: {error_msg}")
            continue
        for exp in found:
            key = exp.get('srr') or exp.get('srx')
            if key in seen_runs:
                continue
            seen_runs.add(key)
            exp['lookup_accession'] = acc
            experiments.append(exp)

    if not experiments:
        if errors and len(errors) == len(lookups):
            return 0, '', 'FAILED', '; '.join(errors)
        ena = {acc: ena_run_count(acc) for acc in lookups}
        if any(n for n in ena.values()):
            detail = ', '.join(f"{a}: {n} runs" for a, n in ena.items() if n)
            return 0, '', 'NO_RUNS', f"NCBI SRA has no runs, but ENA lists {detail} (not mirrored yet)"
        return 0, '', 'NO_RUNS', (f"No public runs for {';'.join(lookups)} in NCBI SRA or ENA "
                                  f"(project registered, reads not released)")

    bioproject = ';'.join(lookups)
    print(f"  Retrieved {len(experiments)} SRA runs", file=sys.stderr)

    # Get unique BioSample IDs
    biosample_ids = list(set(exp['biosample'] for exp in experiments if exp['biosample']))

    if biosample_ids:
        print(f"  Found {len(biosample_ids)} unique BioSamples", file=sys.stderr)
        biosample_metadata = get_biosample_metadata(biosample_ids, email, delay)
    else:
        biosample_metadata = {}

    # Write per-paper TSV
    output_columns = [
        'pmid', 'bioproject', 'sra_experiment', 'sra_run',
        'library_name', 'library_instrument', 'library_strategy', 'library_source',
        'library_selection', 'library_layout',
        'biosample', 'biosample_host', 'biosample_age', 'biosample_sex',
        'biosample_location', 'biosample_strain', 'biosample_isolate',
        'biosample_isolation_source', 'biosample_collection_date',
        'biosample_disease_status', 'biosample_sample_type', 'biosample_other_attributes'
    ]

    try:
        with open(output_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_columns, delimiter='\t')
            writer.writeheader()

            for exp in experiments:
                biosample_id = exp['biosample']
                biosample_attrs = biosample_metadata.get(biosample_id, {})

                row_data = {
                    'pmid': pmid,
                    'bioproject': exp.get('lookup_accession', bioproject),
                    'sra_experiment': exp['srx'],
                    'sra_run': exp['srr'],
                    'library_name': exp['library_name'],
                    'library_instrument': exp['library_instrument'],
                    'library_strategy': exp['library_strategy'],
                    'library_source': exp['library_source'],
                    'library_selection': exp['library_selection'],
                    'library_layout': exp['library_layout'],
                    'biosample': biosample_id,
                    'biosample_host': biosample_attrs.get('host', ''),
                    'biosample_age': biosample_attrs.get('age', ''),
                    'biosample_sex': biosample_attrs.get('sex', ''),
                    'biosample_location': biosample_attrs.get('location', ''),
                    'biosample_strain': biosample_attrs.get('strain', ''),
                    'biosample_isolate': biosample_attrs.get('isolate', ''),
                    'biosample_isolation_source': biosample_attrs.get('isolation_source', ''),
                    'biosample_collection_date': biosample_attrs.get('collection_date', ''),
                    'biosample_disease_status': biosample_attrs.get('disease_status', ''),
                    'biosample_sample_type': biosample_attrs.get('sample_type', ''),
                    'biosample_other_attributes': biosample_attrs.get('other_attributes', '{}')
                }

                writer.writerow(row_data)

        print(f"  Wrote {len(experiments)} samples to {output_filename}", file=sys.stderr)
        return len(experiments), output_filename, 'SUCCESS', '; '.join(errors) or None

    except Exception as e:
        error_msg = f"Failed to write output file: {str(e)}"
        print(f"ERROR: {error_msg}", file=sys.stderr)
        return 0, '', 'FAILED', error_msg


def main():
    parser = argparse.ArgumentParser(
        description='Stage 6: Extract SRA sample metadata from BioProject accessions'
    )
    parser.add_argument(
        '-i', '--input',
        required=True,
        help='Input TSV file (papers_demographics_sra.tsv)'
    )
    parser.add_argument(
        '-o', '--output',
        required=True,
        help='Output TSV file (papers_sra_samples.tsv)'
    )
    parser.add_argument(
        '--sra-dir',
        default='sra_samples',
        help='Output directory for per-paper sample TSV files (default: sra_samples)'
    )
    parser.add_argument(
        '--email',
        default='user@example.com',
        help='Email for NCBI Entrez API (default: user@example.com)'
    )
    parser.add_argument(
        '--delay',
        type=float,
        default=0.34,
        help='Delay between API calls in seconds (default: 0.34 for 3 req/sec)'
    )
    parser.add_argument(
        '--resume',
        action='store_true',
        help='Resume from previous run (skip already processed papers)'
    )

    args = parser.parse_args()

    # Create output directory
    os.makedirs(args.sra_dir, exist_ok=True)

    # Read input TSV
    print(f"Reading input from {args.input}...", file=sys.stderr)

    papers = []
    with open(args.input, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        input_columns = reader.fieldnames
        for row in reader:
            papers.append(row)

    print(f"Found {len(papers)} papers", file=sys.stderr)

    # Add new columns
    output_columns = list(input_columns) + [
        'sra_sample_count',
        'sra_sample_file',
        'sra_extraction_status',
        'sra_extraction_error'
    ]

    # Process each paper
    results = []
    for row in papers:
        sample_count, sample_file, status, error = extract_sra_samples_for_paper(
            row, args.sra_dir, args.email, args.delay, args.resume
        )

        # Update row with results
        row['sra_sample_count'] = str(sample_count) if sample_count > 0 else ''
        row['sra_sample_file'] = sample_file if sample_file else ''
        row['sra_extraction_status'] = status
        row['sra_extraction_error'] = error if error else ''

        results.append(row)

    # Write output TSV
    print(f"\nWriting output to {args.output}...", file=sys.stderr)

    with open(args.output, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=output_columns, delimiter='\t')
        writer.writeheader()
        writer.writerows(results)

    # Summary
    success_count = sum(1 for r in results if r['sra_extraction_status'] == 'SUCCESS' and r['sra_sample_count'])
    total_samples = sum(int(r['sra_sample_count']) for r in results if r['sra_sample_count'])

    print(f"\n{'='*60}", file=sys.stderr)
    print("Stage 6 Complete", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Successfully extracted SRA samples from {success_count} papers", file=sys.stderr)
    print(f"Total samples: {total_samples}", file=sys.stderr)
    statuses = [r['sra_extraction_status'] for r in results]
    for status in ('SUCCESS', 'NO_RUNS', 'SKIPPED', 'FAILED'):
        print(f"  {status}: {statuses.count(status)} papers", file=sys.stderr)
    print(f"Output: {args.output}", file=sys.stderr)
    print(f"Per-paper samples: {args.sra_dir}/", file=sys.stderr)


if __name__ == '__main__':
    main()
