#!/usr/bin/env python3
"""
Paper Discovery Script
Searches PubMed for papers and extracts metadata (abstract, DOI, PMID, authors, etc.)
"""

import argparse
import csv
import sys
from Bio import Entrez


def search_pubmed(query, email, max_results=100, retstart=0):
    """
    Search PubMed with a query string.

    Args:
        query: PubMed search query
        email: Email for NCBI API (required)
        max_results: Maximum number of results to retrieve
        retstart: Starting index for pagination

    Returns:
        List of PubMed IDs (PMIDs)
    """
    Entrez.email = email

    try:
        handle = Entrez.esearch(
            db="pubmed",
            term=query,
            retmax=max_results,
            retstart=retstart,
            sort="relevance"
        )
        record = Entrez.read(handle)
        handle.close()

        print(f"Found {record['Count']} total papers", file=sys.stderr)
        print(f"Retrieving {len(record['IdList'])} papers", file=sys.stderr)

        return record['IdList']
    except Exception as e:
        print(f"Error searching PubMed: {e}", file=sys.stderr)
        return []


def fetch_paper_details(pmid_list, email, batch_size=50):
    """
    Fetch detailed information for a list of PMIDs.

    Args:
        pmid_list: List of PubMed IDs
        email: Email for NCBI API
        batch_size: Number of records to fetch per request

    Returns:
        List of paper records with metadata
    """
    Entrez.email = email
    papers = []

    # Process in batches to avoid API limits
    for i in range(0, len(pmid_list), batch_size):
        batch = pmid_list[i:i+batch_size]
        batch_str = ",".join(batch)

        try:
            handle = Entrez.efetch(
                db="pubmed",
                id=batch_str,
                rettype="medline",
                retmode="xml"
            )
            records = Entrez.read(handle)
            handle.close()

            for record in records['PubmedArticle']:
                paper = extract_metadata(record)
                papers.append(paper)
                print(f"Processed PMID: {paper['pmid']}", file=sys.stderr)

        except Exception as e:
            print(f"Error fetching batch {i//batch_size + 1}: {e}", file=sys.stderr)
            continue

    return papers


def extract_metadata(record):
    """
    Extract relevant metadata from a PubMed record.

    Args:
        record: PubMed article record from Entrez

    Returns:
        Dictionary with paper metadata
    """
    article = record['MedlineCitation']['Article']

    # Extract PMID
    pmid = str(record['MedlineCitation']['PMID'])

    # Extract title
    title = article.get('ArticleTitle', '')

    # Extract abstract
    abstract = ''
    if 'Abstract' in article and 'AbstractText' in article['Abstract']:
        abstract_parts = article['Abstract']['AbstractText']
        if isinstance(abstract_parts, list):
            abstract = ' '.join([str(part) for part in abstract_parts])
        else:
            abstract = str(abstract_parts)

    # Extract authors
    authors = []
    if 'AuthorList' in article:
        for author in article['AuthorList']:
            if 'LastName' in author and 'ForeName' in author:
                authors.append(f"{author['ForeName']} {author['LastName']}")
            elif 'CollectiveName' in author:
                authors.append(author['CollectiveName'])
    authors_str = '; '.join(authors)

    # Extract DOI
    doi = ''
    if 'ELocationID' in article:
        for eloc in article['ELocationID']:
            if eloc.attributes.get('EIdType') == 'doi':
                doi = str(eloc)
                break

    # Extract journal
    journal = article.get('Journal', {}).get('Title', '')

    # Extract publication date
    pub_date = ''
    if 'Journal' in article and 'JournalIssue' in article['Journal']:
        issue = article['Journal']['JournalIssue']
        if 'PubDate' in issue:
            date_parts = issue['PubDate']
            year = date_parts.get('Year', '')
            month = date_parts.get('Month', '')
            day = date_parts.get('Day', '')
            pub_date = f"{year}-{month}-{day}".strip('-')

    # Check if full text is available in PMC
    pmc_id = ''
    if 'PubmedData' in record and 'ArticleIdList' in record['PubmedData']:
        for article_id in record['PubmedData']['ArticleIdList']:
            if article_id.attributes.get('IdType') == 'pmc':
                pmc_id = str(article_id)
                break

    # Generate PubMed URL
    pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

    return {
        'pmid': pmid,
        'pubmed_url': pubmed_url,
        'pmc_id': pmc_id,
        'doi': doi,
        'title': title,
        'abstract': abstract,
        'authors': authors_str,
        'journal': journal,
        'pub_date': pub_date
    }


def save_to_csv(papers, output_file):
    """
    Save paper metadata to a CSV file.

    Args:
        papers: List of paper dictionaries
        output_file: Output CSV file path
    """
    if not papers:
        print("No papers to save", file=sys.stderr)
        return

    fieldnames = ['pmid', 'pubmed_url', 'pmc_id', 'doi', 'title', 'abstract', 'authors', 'journal', 'pub_date']

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter='\t')
        writer.writeheader()
        writer.writerows(papers)

    print(f"Saved {len(papers)} papers to {output_file}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description='Search PubMed and extract paper metadata',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  python paper_discovery.py -q "saliva microbiome sequencing dental"
  python paper_discovery.py -q "oral microbiome" -n 50 -o oral_papers.tsv
        """
    )

    parser.add_argument('-q', '--query', required=True, help='PubMed search query')
    parser.add_argument('-e', '--email', default='user@example.com',
                        help='Email address for NCBI (default: user@example.com)')
    parser.add_argument('-n', '--max-results', type=int, default=100,
                        help='Maximum number of results (default: 100)')
    parser.add_argument('-o', '--output', default='papers.tsv',
                        help='Output TSV file (default: papers.tsv)')
    parser.add_argument('--retstart', type=int, default=0,
                        help='Starting index for pagination (default: 0)')

    args = parser.parse_args()

    print(f"Searching PubMed for: {args.query}", file=sys.stderr)

    # Search PubMed
    pmid_list = search_pubmed(args.query, args.email, args.max_results, args.retstart)

    if not pmid_list:
        print("No papers found", file=sys.stderr)
        sys.exit(1)

    # Fetch details
    papers = fetch_paper_details(pmid_list, args.email)

    # Save to CSV
    save_to_csv(papers, args.output)

    print(f"Done! Retrieved {len(papers)} papers", file=sys.stderr)


if __name__ == '__main__':
    main()
