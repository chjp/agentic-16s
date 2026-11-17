# What the first 5 studies taught us

Five IBD papers that reached SRA samples in the 100-paper test run, each curated by one
Claude Code agent into a run-level sample sheet (2026-11-15). Per-study details are in each
folder's `log.jsonl`, `validation.json` and (where written) `summary.md`.

## Results

16S runs only. "Group" = case/control known; sex and age per run.

| PMID | Study | 16S runs | Group | Sex | Age | Verdict | Where the labels lived |
|---|---|---|---|---|---|---|---|
| [35756051](https://pubmed.ncbi.nlm.nih.gov/35756051/) | UC ± C. difficile, stool | 89 | 89 | 89 | 89 | complete, 16/18 checks | BioSample sample-name prefix (HC/UCN/UCP); `sex`, `age` attributes |
| [28174747](https://pubmed.ncbi.nlm.nih.gov/28174747/) | Pediatric IBD + relatives, stool | 90 | 90 | 90 | 0 | complete, 34/34 checks | BioSample `gastrointest_disord`, `host_sex`, `host_subject_id` |
| [29335008](https://pubmed.ncbi.nlm.nih.gov/29335008/) | Pediatric CD biopsies (+75 WGS runs) | 40 | 40 | 40 | 40 | complete, 18/18 checks | BioSample `disease state`; sex/age from supplement xlsx joined on S1–S40 |
| [37661264](https://pubmed.ncbi.nlm.nih.gov/37661264/) | CD stool + mucosa | 97 | 97 | 0 | 0 | partial | SRA experiment title (`CD_16S_mucosa`); inflamed vs uninflamed unknown |
| [23013615](https://pubmed.ncbi.nlm.nih.gov/23013615/) | Morgan 2012, stool + biopsy | 257 | 228 | 0 | 228 | partial, 65/99 checks | Authors' metadata file, found only as a Zenodo copy (MicrobiomeHD) |
| **Total** | | **573** | **544 (95%)** | **219 (38%)** | **357 (62%)** | | |

Case/control is recoverable for almost every run. Sex and age often are not: papers give them
only as group totals.

## Where labels live: five studies, five different places

1. BioSample attributes with study-specific names (`gastrointest_disord`, `disease state`).
2. The BioSample sample name, as a group prefix.
3. The SRA experiment title.
4. A supplement spreadsheet keyed on the archive's sample alias.
5. A file outside the paper altogether (the lab's website, dead; a copy on Zenodo).

No single rule finds them. Labels never came from the paper's main text; the paper is
where the **counts** come from, which is what makes validation possible.

## Archive lessons (scriptable)

- **Fetch full records by accession.** SRA runinfo and the ENA filereport left the label
  columns empty in 3 of 5 studies; the labels were only in the BioSample XML and the SRA
  experiment XML (titles, aliases). Our pipeline's BioSample parser also missed custom
  attributes in 2 studies.
- **`esearch db=biosample` by BioProject returned 0 hits in 3 of 5 studies** (including
  EBI-brokered samples). Take the SAMN/SAMEA list from runinfo and fetch those.
- **elink with comma-separated IDs merges results**; send repeated `id=` parameters.
- **Archives hold runs the paper did not use** (excluded samples, pilot runs, a pooled
  "unmatched barcodes" run) and mix 16S with shotgun in one BioProject. Keep every run,
  flag `is_16s` and `used_in_paper`.
- **Submitters' workarounds leak into metadata**: ages nudged to 57.1/57.2 to pass
  BioSample's duplicate check; collection dates spread artificially in accession order.

## Paper and supplement lessons

- **Best route to full text and supplements: the PMC open-data S3 bucket**
  (`pmc-oa-opendata`), which worked when Europe PMC returned 403 or a Cloudflare challenge
  and PMC's old OA service returned 404. Europe PMC's REST `supplementaryFiles` zip also
  worked. Worth testing as the pipeline's main source.
- **Key tables are often images inside PDFs.** The agents could not render PDF pages
  (`pdftoppm` is not installed; `pymupdf` failed to build) and pulled embedded images out
  with pypdf instead. Installing poppler would make this routine.
- **Counts hide in supplement prose** (54 CD + 14 controls appeared only in a docx).

## Validation lessons

- **Exact read-count equality is a cheap, strong join check**: archive read counts equalled
  the supplement's per-sample counts 40/40 in one study and matched the paper's depth
  statistics exactly in another.
- **"Participants" may mean samples** (231 "patients" were 221 people), and "controls" may be
  healthy relatives of the patients. Keep subject_id separate from sample, and keep the
  paper's own group names.
- **Every study had at least one count the sheet could not reproduce**, usually off by one to
  three. The checks localise the problem, not hide it.
- The earlier Haiku extraction got the headline counts right in all 5 studies.

## What needs a person

- 37661264: which of the 45 biopsies are inflamed, and how they pair by patient. Only the
  authors know.
- 23013615: sex is coded the opposite way to the paper's Table 1 in every group; left empty.
  Also: accept a third-party copy (Zenodo) of author metadata as a source?
- 28174747: controls are healthy relatives of patients. Pool them as controls or not?
- 35756051: does "UC with C. difficile infection" count as a plain UC case?
- 29335008: transcribed image tables need a spot-check.

## Proposed changes to the workflow

1. Replace the archive step with a full-record fetch: runinfo for the run list, then SRA
   experiment XML and BioSample XML by accession, flattened into `sra_runs.tsv` with every
   attribute as a column.
2. Try the PMC open-data bucket for full text and supplements before Europe PMC.
3. Give the label-finding agent these tools, in this order: archive attributes; name and
   title patterns; supplement spreadsheets joined on an archive ID; external copies of author
   metadata (Zenodo, Qiita, MicrobiomeHD, lab pages).
4. Make validation code, not agent judgement: a structured "counts stated in the paper"
   table compared against the sheet, plus read-count join checks.
5. Send the items above, and anything an agent marks "only the authors can fix", to the
   human queue after the MVP.
