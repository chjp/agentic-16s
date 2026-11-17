# pipeline/ — 16S reads to ASV tables (Phase 3)

One uniform, pinned workflow for every study: download the runs, remove primers, denoise with
DADA2, classify against SILVA, and write tables shaped like the `bio.*` tables in
[`schema-design.html`](../schema-design.html). The heavy lifting is
[nf-core/ampliseq](https://nf-co.re/ampliseq) and [nf-core/fetchngs](https://nf-co.re/fetchngs).
This folder adds what they can't know: what each study's reads actually look like, which
parameters follow from that, and what goes into the database.

```
studies/<PMID>/sample_sheet.tsv ─┐
studies/<PMID>/protocol.json  ───┤  prepare ─► preflight ─► fetch ─► ampliseq ─► collect ─► qc ─► load ─► cleanup
ENA file report  ────────────────┘   manifest   params.json  FASTQ    ASVs       tables/    verdict  bio.*   FASTQ deleted
```

## Run it

```bash
python3 -I pipeline/bin/run_study.py 35756051 --subset 6     # smoke run: 6 runs, all stages
python3 -I pipeline/bin/run_study.py 35756051                 # whole study
python3 -I pipeline/bin/run_study.py 35756051 --from ampliseq # resume from a stage
python3 -I pipeline/bin/preflight.py 37661264                 # preflight only (needs prepare first)
uv run --with pytest pytest pipeline/tests                    # unit tests
```

Requirements: Nextflow 26.10.6 (`brew install nextflow`), Miniforge
(`brew install --cask miniforge`, for `mamba`), and for studies whose ENA FASTQ lost the quality
scores, sra-tools in a project conda env
(`mamba create -p $DATA_DIR/conda/sra-tools --override-channels -c conda-forge -c bioconda sra-tools`).
The `load` stage needs PostgreSQL (`DATABASE_URL` in `.env`, see [`db/README.md`](../db/README.md)) and `uv`. **No Docker**: on the M4 Mac mini (16 GB) every
step runs in a conda environment built natively for osx-arm64. The nf-core containers are
amd64-only, and a Docker VM that is big enough would take most of the Mac's memory (a 4 GB VM
under emulation hung once). The environments are built on first use and cached in
`DATA_DIR/conda`. Caps per run come from `resources.config`, which `run_study.py` writes: at most
half the physical memory, and all CPUs but two. On a Linux server, set
`NF_PROFILE=docker` (or `singularity`) in [`versions.env`](versions.env), where all versions
are pinned. Large files go to `DATA_DIR` from `.env` (default `~/agentic16s-data`, outside Desktop
so iCloud doesn't sync them).

## Stages

| Stage | Script | Writes (under `DATA_DIR/pipeline/<PMID>/<tag>/` unless noted) |
|---|---|---|
| prepare | `make_samplesheet.py` | `manifest.tsv` (run, layout, ENA URL, md5, bytes), `runs.csv` (one ID per line, no header, as fetchngs expects). Runs the database marks `skipped_not_used` or `skipped_not_16s` are left out |
| preflight | `preflight.py` | `preflight.json` (what the reads look like), `params.json` (ampliseq parameters, each with a reason) |
| fetch | nf-core/fetchngs, or `sra_source.py` | FASTQ under `DATA_DIR/fastq/<PMID>/<tag>/`, md5-checked; then `samplesheet.tsv`. When preflight chose `fastq_source = ncbi_sra_normalized`: NCBI's .sra per run (md5-checked), `fastq-dump --clip`, .sra deleted |
| ampliseq | nf-core/ampliseq | `ampliseq/` (DADA2 tables, reports, `pipeline_info/`), `ampliseq_run.json` |
| collect | `collect.py` | `tables/`: `pipeline_run.json`, `run_qc.tsv`, `asv.tsv`, `abundance.tsv`, `taxonomy_assignment.tsv`, `integrity.json` |
| qc | `qc_gate.py` | `studies/<PMID>/pipeline_qc.json` (committed; `pipeline_qc_subsetN.json` for smoke runs) |
| load | [`db/load_bio.py`](../db/load_bio.py) | `bio.pipeline_run` (is_current), `bio.run_qc`, `bio.asv`, `bio.abundance`, `bio.taxonomy_assignment`, `core.run` states, `ops.activity_log`. Skipped for `--subset` runs and with `--no-db` |
| cleanup | `run_study.py` | deletes the raw FASTQ, see below |

`<tag>` is the protocol label (`main`), or `main_subset6` for a smoke run. Every stage appends a
line to `studies/<PMID>/log.jsonl`.

## Preflight: check the reads before trusting the paper

Preflight streams the first 2000 reads of 8 runs straight from ENA (a few MB, no download) and
compares them with `protocol.json`. Its decisions:

| Observation | Decision |
|---|---|
| Stated primers at the start of ≥ 80 % of reads (full match within 12 nt, or a 5'-clipped primer suffix ≥ 10 nt, about 10 % mismatches allowed, like Cutadapt) | Cutadapt with `primer_fwd`/`primer_rev` |
| Primers in < 5 % of reads | `skip_cutadapt`: the archive reads are already trimmed |
| Anything in between, mixed orientation, binned quality scores | `needs_review`: stop and say why |
| Quality scores lost (≤ 2 distinct values; ENA serves FASTQ made from SRA Lite) | Look up NCBI's normalized .sra of the sampled runs (SDL locator, AWS Open Data). If every one has real scores and the same reads, fetch all runs from there (`fastq_source`); otherwise `needs_review` |
| Paper gives no primers | Adopt the catalog pair ([`assets/primers_16s.tsv`](assets/primers_16s.tsv)) found in ≥ 80 % of reads, recorded as `inferred:preflight catalog match` |
| Single-end reads start with the reverse primer, or conserved 16S sites show them reversed | Swap the primers, `dada_taxonomy_rc`; `collect.py` flips the ASVs back to gene orientation |
| 454 / Ion Torrent | `iontorrent` mode only if ≥ 80 % of reads span both primers; otherwise single-end plus DADA2 homopolymer settings (`extra.config`) and a fixed `trunclenf`: the length 75 % of reads reach, capped where median quality drops below 25 (ampliseq left `truncLen = 0` here, which split one organism into ASVs of many lengths) |
| Paired reads: ampliseq's automatic truncation would leave < 32 nt overlap for the expected amplicon (E. coli length + 20) | Explicit `trunclenf`/`trunclenr` that keep the overlap; review if the reads can't reach it at all |

What it found in the five curated IBD studies:

| PMID | Reads | Preflight result |
|---|---|---|
| 35756051 | MiSeq 2×301, V3-V4 341F/785R | Primers present (97 % / 95 %, 0–3 nt spacers). Auto truncation would leave 17 nt overlap → `trunclenf 281`, `trunclenr 178` |
| 37661264 | MiSeq 2×250, V3-V4, primers not in paper | Catalog match 341F (Novogene) / 806R: 86 % / 95 % of reads, counting the many 5'-clipped primers (`TACGGGAGGCAG…`) that Cutadapt also trims. Predicted overlap 34 nt, automatic truncation kept |
| 29335008 | MiSeq 2×301, V6-V8 | Primers present (98 %); automatic truncation leaves enough overlap |
| 28174747 | HiSeq 2000 1×101, V4 (EMP) | No primers in the reads (they start `TACGGAGG`/`TACGTAGG`, right after 515F) → `skip_cutadapt` |
| 23013615 | 454 Titanium, V3-V5 | Primers trimmed, 87 % of reads reversed (sequenced from 926R). ENA's quality scores replaced (1–2 values); NCBI's normalized .sra has 34, same reads → fetched from NCBI, `trunclenf 280` (260 on the 6-run smoke subset). Full run 2026-11-15: 256 runs, 2544 ASVs, verdict review (4 runs keep < 20 % in filtering), loaded into `bio.*` |

## Cleanup: raw FASTQ is deleted once processing is complete

On by default; `--keep-fastq` turns it off.

1. Nextflow `cleanup = true` ([`conf/local_mac.config`](conf/local_mac.config)): when a fetchngs
   or ampliseq run succeeds, Nextflow empties its `work/` (the work copy of each download, and the
   trimmed and filtered reads). A failed run keeps `work/`, so `-resume` still works.
2. The `cleanup` stage deletes the downloaded FASTQ, but only if these checks pass. Runs that failed
   keep their FASTQ for debugging.
   - `db/load_bio.py --check`, when the study was loaded: each run's `reads_input` equals the
     archive `spots`, and its abundance sum equals the non-chimeric reads in the database
   - ampliseq exited 0
   - every samplesheet run has a `run_qc` row
   - per-run sum of abundance counts equals DADA2's non-chimeric reads
   - every ASV md5 matches its sequence and has a taxonomy row

   If any check fails, the FASTQ is kept and the log says why. A QC verdict of `review` doesn't
   block deletion: a rerun fetches the reads again from the ENA URL and md5 in `manifest.tsv`.
3. Kept: `manifest.tsv`, `samplesheet.tsv` and its sha256, `tables/`, the ampliseq reports and
   `pipeline_info/`. The deletion is logged (file count, GB freed) and stamped into
   `pipeline_run.json` as `raw_fastq_deleted_at`.

## Fixed settings (every study)

These settings are the same for every study, and each one has its reason in `params.json`:
- SILVA (`dada_ref_taxonomy silva=144`), with the reference cached in `DATA_DIR/refdb`.
- `exclude_taxa none`: mitochondria and chloroplasts stay in the tables and are filtered in the
  analysis views, so counts add up.
- Failed runs don't stop a study (`ignore_failed_*`).
- Seed 100.
- Skipped: QIIME2, the phyloseq and TreeSummarizedExperiment R objects, and the R Markdown summary
  report. The database tables are the output. MultiQC plus `pipeline_qc.json` cover QC.

## Apple silicon (osx-arm64) with conda

On 2026-11-14 every ampliseq 3.1.0 module environment was dry-run solved for osx-arm64 against
conda-forge and bioconda: 71 of 82 solve as pinned. The fixes for the ones on our path are in
[`conf/ampliseq_osx_arm64.config`](conf/ampliseq_osx_arm64.config):
- The Cutadapt summary step pins `python=3.8.3`, which has no osx-arm64 build. It gets
  `python=3.8.20` (its script only imports `re` and `sys`).
- phyloseq, TreeSummarizedExperiment and the summary report don't solve, or fail at run time
  (`rlang.dylib`), so they are skipped.
- The other 8 that don't solve belong to features we don't run (QIIME2 export, decontam, ITS,
  length filters, phylogenetic placement).

Two more fixes came from the first full run (23013615, 2026-11-15):
- **addSpecies memory.** ampliseq runs dada2's `addSpecies` with `n = 1e5`, which matches all
  ASVs of one length against all 1.47M SILVA species references in one go. After the fixed 454
  truncation every ASV is 280 nt, so a 2000-ASV file hit R's 16 GB limit, and `n = 200` still did.
  Measured: peak ≈ refs × (3430 + 23.3 × n) bytes, about 45 s per chunk. `run_study.py` now sets
  `n` in `resources.config` from the memory cap (`n = 70` at 8 GB).
- **`-resume` silently missed the cache.** For single-end files already named `<run>.fastq.gz`,
  ampliseq's `RENAME_RAW_DATA_FILES` runs `touch`, which follows the symlink and changes the raw
  FASTQ's mtime. Nextflow's default cache key includes the mtime, so every task reran (70 min
  to get back to the failed step). [`conf/local_mac.config`](conf/local_mac.config) sets
  `process.cache = 'lenient'` (path + size), which is safe because downloads are md5-checked.

`conda.createOptions = '--override-channels'` keeps mamba to the channels each environment
file names, so a user-level `~/.condarc` that lists Anaconda's commercial `defaults` channel is
ignored. Checked: no installed package comes from `repo.anaconda.com`.

## QC gate

`qc_gate.py` sets the verdict to `review` when any of these holds:
- More than 10 % of runs failed.
- Median primer-found is below 80 % (when Cutadapt ran).
- Median quality-filter retention is below 60 %, or any single run keeps less than 20 %.
- Median merged/denoised is below 0.70.
- Less than 90 % of reads sit in ASVs of plausible length (expected amplicon −40/+20 nt).
- More than 10 % of reads are unassigned at phylum level.
- The integrity checks failed.

A `review` verdict lists its reasons; the data stay in the tables.

## Smoke run (2026-11-15)

35756051, 6 runs (3 UC, 3 healthy controls), all stages on the Mac mini with conda:
- 0.16 GB downloaded and md5-checked, then deleted after the integrity checks passed. Nextflow
  `work/` was emptied too.
- Primers found in 95–98 % of reads; automatic truncation 280/185 (preflight predicted the same);
  merge rate 0.98.
- 389 ASVs, 99.9 % of reads in ASVs of 402–427 nt, nothing unassigned at phylum level.
- Per-run abundance sums equal DADA2 non-chimeric reads.
- Verdict **review**: SRR15400843 keeps only 3 % of its reads in quality filtering (maxEE 2 on a
  poor reverse read). The other five keep 47–82 %.
- Sanity check: Bacteroides dominates, and Faecalibacterium is lower in UC than in controls
  (3.6 % vs 7.3 %).
- One-time costs: conda environments 4.8 GB, SILVA reference 337 MB.
