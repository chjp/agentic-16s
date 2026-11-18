# discovery/: from a disease name to papers with public reads

The literature end of the pipeline: sample papers, screen their abstracts with an LLM, fetch
open-access full text and supplements, read the accessions out of the paper, and look the runs
up in the archives. Each stage is one script that reads the previous stage's TSV and writes its own.

The code came from the separate `paper-discover2sra` repository at commit `8c602c0`
(2026-11-17), which is now frozen; all further work happens here.

## Stages

| Script | In | Out |
|---|---|---|
| `sample_epmc.py` | Europe PMC records (`phase0/data/epmc_records_<disease>.jsonl.gz`) | `papers.tsv`, a seeded random sample; `--exclude` leaves out earlier batches |
| `paper_discovery.py` | a PubMed query | `papers.tsv` (alternative to sampling) |
| `abstract_screening.py` | `papers.tsv` + `criteria/<disease>_criteria.txt` | `papers_screened.tsv`: INCLUDE / EXCLUDE / UNCERTAIN with a reason |
| `fulltext_download.py` | `papers_screened.tsv` | `papers_fulltext.tsv` + `fulltext_xml/` (PMC open access only; others NO_PMC / NOT_OA, see `pipeline/bin/fetch_paper.py`) |
| `supplementary_download.py` | `papers_fulltext.tsv` | `papers_supplementary.tsv` + `supplementary_files/` |
| `demographic_sra_extraction.py` | `papers_fulltext.tsv` | `papers_demographics_sra.tsv`: accessions, groups, sample type, platform, region |
| `sra_sample_extraction.py` | `papers_demographics_sra.tsv` | `papers_sra_samples.tsv` + `sra_samples/` (runs and BioSample attributes) |
| `build_race.py` | a run folder | `race.html`: one runner per paper, stopping at the first stage it cannot pass |

The two LLM stages call `claude -p --model haiku` through `llm_cli.py` on the Claude login of whoever
runs them (no API key; `--backend gemini` also works). Every raw reply is stored under
`runs/<run>/llm_raw/<stage>/<pmid>.json` with a hash of its prompt and reused only while the prompt is
unchanged. The cost the CLI reports is the API-price equivalent; nothing is billed on a subscription login.

## A run

Run from the repository root; run folders live in `discovery/runs/` (git-ignored: paper text is never
committed).

```bash
R=discovery/runs/ibd200b; mkdir -p $R
python3 discovery/sample_epmc.py -i phase0/data/epmc_records_ibd.jsonl.gz -n 200 --seed 17 --exclude discovery/runs/ibd100/papers.tsv -o $R/papers.tsv
python3 discovery/abstract_screening.py -i $R/papers.tsv -o $R/papers_screened.tsv --criteria-file discovery/criteria/ibd_criteria.txt --raw-dir $R/llm_raw
```

The NCBI stages need Biopython: `uv run --group discovery python discovery/<script>.py ...`.

Screening rules that matter (and their tests in `tests/`): the criteria say what the project needs
(stool 16S for the MVP); the model writes its reason before its decision; an INCLUDE whose own reason
argues for exclusion becomes UNCERTAIN (`docs/failure-taxonomy.md`, Q23).
