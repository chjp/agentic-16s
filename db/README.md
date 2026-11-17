# db/

PostgreSQL database for agentic-16s, implementing schema draft v0.2 (`../schema-design.html`).

## Run it

PostgreSQL 17 from Homebrew runs as a background service; the database is `agentic16s` on the
local socket, no password. `DATABASE_URL` is in `../.env`.

```bash
brew services start postgresql@17                                   # if it is not running
uv run --with 'psycopg[binary]' python db/migrate.py                # apply new migrations
uv run --with 'psycopg[binary]' python db/load_study.py             # (re)load every studies/<PMID>/
uv run --with 'psycopg[binary]' python db/load_study.py studies/35756051   # or one study
uv run --with 'psycopg[binary]' python db/fetch_fastq_links.py       # FASTQ URLs, MD5, sizes from ENA (rerun after loading)
/opt/homebrew/opt/postgresql@17/bin/psql -d agentic16s -f db/queries/overview.sql
```

- `migrate.py` applies `migrations/NNN_*.sql` once each, in order, recorded in `ops.schema_migration`.
  Never edit an applied migration; add a new one.
- `load_study.py` is idempotent: it deletes everything belonging to a study and reloads it in one
  transaction. Curator knowledge not in the study files (group definitions, which controls are
  relatives, open questions for people) is in `STUDY_NOTES` at the top of the script.

**Browse in the web page** (read-only Streamlit app, `dashboard/app.py`):

```bash
uv run --with streamlit --with 'psycopg[binary]' --with pandas streamlit run dashboard/app.py --server.address localhost
```

Then open http://localhost:8501. Pages: Overview, Studies (groups, checks against the paper,
protocol, documents, activity log), Samples (click a row to see every value and its source),
Cohorts, Fields, Open questions, Bioinformatics. The first start takes about 20 seconds.

**IBD vs healthy model** (Phase 6.1): genus counts per sample are the materialized view
`analysis.genus_counts` (refresh it after each `load_bio.py`; `ml/loso.py` refreshes it before reading).
Leave-one-study-out results and how to read them: [`docs/ml-results.md`](../docs/ml-results.md). The dashboard's **Model: IBD vs healthy** page shows the same results as charts (pick a result set at the top).

```bash
uv run --with scikit-learn --with pandas --with 'psycopg[binary]' python ml/loso.py                     # all sample types
uv run --with scikit-learn --with pandas --with 'psycopg[binary]' python ml/loso.py --sample-type stool  # stool only
```

**Browse with DBeaver**: New Database Connection → PostgreSQL → Host `localhost`, Port `5432`,
Database `agentic16s`, Username your macOS user name, password empty. Start with the views in
`analysis` and the tables `core.study_group`, `core.observation`, `ops.human_task`.

## Layout

| Schema | Holds |
|---|---|
| `raw` | Verbatim inputs: `api_record` (files and API responses), `archive_attribute` (every archive field, one row per run and column) |
| `core` | Facts with provenance: paper, study, study_group, reported_count, subject, sample, run, observation (every value and its source; conflicts kept), field_dictionary, value_map, document, extraction |
| `bio` | Bioinformatics output, empty until the workflow runs |
| `ops` | activity_log, human_task, validation_check |
| `analysis` | `cohort_definition` (the only place case/control is decided) and views `sample_wide`, `ml_samples`, `samplesheet_16s`, `study_progress` |

## Contract for the bioinformatics workflow

The Nextflow / nf-core/ampliseq workflow is built in a separate session. It talks to the
database only through these:

**Reads** `analysis.samplesheet_16s`: one row per 16S run still to process
(`processing_state` queued or failed), with `study_key`, `protocol_id`, `variable_region`,
primers (when known), `platform`, `layout`, `spots`, `fastq_urls`, `fastq_md5`, `fastq_bytes`.
Runs are processed per `protocol_id` (one region and platform at a time). Shotgun runs and
runs the authors did not use are already excluded (`skipped_not_16s`, `skipped_not_used`).
`variable_region` currently comes from an unverified Haiku extraction; check it against the
reads (primer search) before trusting it.

**Writes**
- `bio.reference_db`: one row per reference database and version used.
- `bio.pipeline_run`: one row per execution (study, protocol, pipeline version, all params,
  containers, status). Set `is_current = true` on the run the analysis should use (one per protocol).
- `bio.run_qc`: reads surviving each step per run, `pct_primer_found`, status ok / low_depth / failed.
- `bio.asv` (global, deduplicated by `sequence_md5`), `bio.abundance` (raw counts > 0, keyed by
  pipeline run, run accession, ASV), `bio.taxonomy_assignment`.
- `core.run`: `processing_state` → processed or failed; `download_state`, `fastq_local_path`,
  `fastq_deleted_at` as below.
- `ops.activity_log`: one row per batch (actor_type `script`, actor the workflow name and version).

**Raw FASTQ is deleted once processed** (required step):
1. Download a study's runs into `$DATA_DIR/fastq/<study_key>/`; check each file against `fastq_md5`
   when present; set `download_state = downloaded`, `fastq_local_path`.
2. Run the pipeline; load `bio.run_qc` and `bio.abundance` for every run.
3. Only for runs whose results are in the database and consistent (a `run_qc` row exists,
   abundance rows exist, `reads_input` equals the archive `spots`): set `processing_state = processed`,
   delete the FASTQ and the Nextflow `work/` copies of that run, then set
   `download_state = deleted`, `fastq_deleted_at = now()`. Keep ASV tables, QC and MultiQC reports.
4. Failed runs keep their FASTQ (`processing_state = failed`) for debugging. `KEEP_FASTQ=true`
   disables deletion.
5. Process in batches that fit the free disk space.

The database enforces part of this: a run cannot be marked `deleted` unless it is `processed`,
and `deleted` requires `fastq_deleted_at`. Everything can be re-fetched from `fastq_urls`.

Suggested first study for a smoke run: `ibd-2022-35756051` (89 runs, V3-V4, Illumina MiSeq, paired).
