# Experience log

What went wrong or surprised us when running the pipeline, what it cost, how it was fixed, and
what it means at larger scale. Its main use is sizing machines when the project scales up
(for example the Google Cloud worker in [`gcp-migration.md`](gcp-migration.md)). Its second use is
as material for [`demo-stories.md`](demo-stories.md).

Rules:
- Add an entry when a run fails, needs a rerun, or uses far more time or memory than expected.
  Record it on the day it happens.
- Give numbers: wall time lost, peak memory, data size. Say where they came from (measured,
  sampled, or estimated).
- Measured resource use per step is written by the pipeline itself to
  `studies/<PMID>/resource_profile.json`, failed runs included (see
  [`pipeline/bin/resource_monitor.py`](../pipeline/bin/resource_monitor.py)). This file holds the
  interpretation. For a table over all studies:

```bash
python3 -I pipeline/bin/resource_monitor.py report
```

Entry template:

```
### E<n>. <short title> (<date>, <PMID or "all">)
Symptom · Cause · Cost · Fix · Scale-up note · Evidence
```

---

## Index

| # | Date | Area | Short | Cost |
|---|---|---|---|---|
| E1 | 2026-11-14 | infra | nf-core containers are amd64-only; Docker on the M4 Mac hung | switched to conda |
| E2 | 2026-11-14 | infra | 11 of 82 ampliseq conda envs don't solve on osx-arm64 | 3 steps patched or skipped |
| E3 | 2026-11-15 | data | ENA FASTQ of a 454 study has no real quality scores | preflight stop, then NCBI .sra fallback |
| E4 | 2026-11-15 | method | 454 reads with `truncLen = 0` give one ASV per read length | one smoke run (review verdict) |
| E5 | 2026-11-15 | memory | DADA2 addSpecies ran out of R's 16 GB, twice | 2 full runs, ~2 h 40 min |
| E6 | 2026-11-15 | infra | `-resume` silently reran every task of a single-end study | ~70 min per rerun |
| E7 | 2026-11-15 | infra | Nextflow records no memory or CPU on macOS | built a `ps` sampler |
| E8 | 2026-11-15 | ops | `brew install` blocked by the permission check | used a conda env instead |
| E9 | 2026-11-15 | infra | `resource_monitor` crashed on Python 3.12 (GCE), never on 3.9 (Mac) | one VM attempt |
| E10 | 2026-11-15 | data | ENA lists a FASTQ with size and md5 but serves an HTML page | 27 min, then the study waited overnight |
| E11 | 2026-11-15 | compute | pooled DADA2 denoising: ~1 h 45 min per read direction on 3 of 8 vCPUs | hours of the GCE batch |
| E12 | 2026-11-16 | memory | a fixed `memory` for an nf-core step turns off its retry; exit 137 | 29 min run lost |
| E13 | 2026-11-16 | ops | code pushed to the VM did not reach a `run_study.py` already running | 2 h 13 min run failed |
| E14 | 2026-11-16 | compute | addSpecies chunks (15–17 GB each) run one at a time on 32 GB | ~5 h of one study; step now skipped |
| E15 | 2026-11-16 | method | QC length check flags 1×101 reads as wrong-length | false `review` verdict |
| E16 | 2026-11-16 | infra | whole-pipeline memory peak is undercounted under Docker | sizing must use per-task peaks |

---

### E1. nf-core containers are amd64-only (2026-11-14, all)

- **Symptom:** under Docker Desktop on the M4 Mac mini (16 GB), a 4 GB VM running amd64 images
  under emulation hung.
- **Cause:** nf-core images are amd64-only. A Docker VM big enough for DADA2 would take most of
  the Mac's memory.
- **Fix:** every step runs in a conda/mamba environment built natively for osx-arm64
  (`NF_PROFILE=mamba`).
- **Scale-up note:** on a Linux x86 cloud VM use `NF_PROFILE=docker` (pinned images). The Mac's
  conda environments are not the same builds, so check results from both platforms against each
  other once (same study, same params).
- **Evidence:** `pipeline/README.md` (Run it), `pipeline/versions.env`.

### E2. ampliseq conda environments on osx-arm64 (2026-11-14, all)

- **Symptom:** a dry-run solve found that 71 of 82 module environments solve as pinned.
- **Fix:**
  - Cutadapt summary: Python 3.8.3 changed to 3.8.20.
  - phyloseq, TreeSummarizedExperiment and the summary report: skipped.
  - `--override-channels`, so the commercial Anaconda `defaults` channel from `~/.condarc` is never used.
- **One-time cost:** conda environments 4.8 GB, SILVA reference 337 MB.
- **Evidence:** `pipeline/conf/ampliseq_osx_arm64.config`.

### E3. Archived reads lost their quality scores (2026-11-15, 23013615)

- **Symptom:** ENA FASTQ has only 2 distinct quality values. Preflight stopped the study before
  any download.
- **Cause:** for old runs, ENA serves FASTQ made from SRA Lite, which replaces the qualities with
  constants. NCBI still has the full "normalized" .sra (AWS Open Data, free).
- **Fix:** a general preflight rule. When the qualities are lost, check NCBI's normalized .sra
  for the sampled runs. If those have real scores and the same reads, fetch every run from there.
  - Result for this study: 34 quality values; sequences identical to ENA for all 4950 reads
    checked.
  - Download: 1.58 GB of .sra for 256 runs, about 2.5 min with 4 parallel downloads (each .sra
    deleted after dumping).
- **Scale-up note:** expect this for any pre-2015 454 / Ion Torrent study. The .sra is about
  30× the size of ENA's gzipped FASTQ (1.58 GB vs 51 MB here), so the download budget per study
  goes up, not the compute.
- **Evidence:** `docs/demo-stories.md` story 1, `pipeline/bin/sra_source.py`,
  `studies/23013615/log.jsonl`.

### E4. 454 reads need one fixed truncation length (2026-11-15, 23013615)

- **Symptom:** smoke run 1 got verdict `review`:
  - modal ASV length 284;
  - only 26 % of reads in ASVs of plausible length;
  - 15 % of reads unassigned at phylum level;
  - DADA2_ADDSPECIES took 22 min for 6 runs.
- **Cause:** ampliseq left `truncLen = 0` for single-end 454. Reads of varying length gave one
  organism many ASVs, and short junk passed `minLen 50`.
- **Fix:** preflight sets `trunclenf` to the length 75 % of reads reach, capped where median
  quality drops below 25 (260–280 nt here). Smoke run 2 passed:
  - 100 % of reads in ASVs of plausible length;
  - 2.2 % unassigned at phylum level;
  - ampliseq took 6.5 min.
- **Scale-up note:** this fix is what triggered E5. Fixed-length ASVs put all of them in one
  addSpecies batch.

### E5. DADA2 addSpecies runs out of memory (2026-11-15, 23013615)

- **Symptom:** the full run (256 runs, 2544 ASVs in 2 files of 2000 + 544) failed at
  DADA2_ADDSPECIES with `vector memory limit of 16.0 Gb reached`:
  - run 1 failed after 76 min, with ampliseq's `n = 1e5`;
  - run 2 failed after 85 min, with my estimated `n = 200`.

  The 544-ASV file passed both times (4 min).
- **Cause:** dada2 compares every ASV of one length against all 1,466,213 SILVA species
  references in one matrix, and does it again for the reverse complement. With fixed-length ASVs
  (E4), one chunk held up to `n` ASVs.
- **Measured** (R 4.2 / dada2 1.30 on the Mac, `tryRC = TRUE`, `gc()` max plus
  `/usr/bin/time -l`):

  | ASVs per chunk (`n`) | max R vector memory | peak footprint | time |
  |---|---|---|---|
  | 50 | 6.5 GB | 8.1 GB | 1.3 min incl. reading refs |
  | 100, 3 chunks | 8.2 GB | 9.9 GB | 3.1 min (~45 s/chunk) |

  Cost model: peak ≈ refs × (3430 + 23.3 × n) bytes. That is about **4.8 GB fixed plus 34 MB per
  ASV in a chunk**. Memory does not grow across chunks.
- **Fix:** `run_study.py` computes `n` from the memory cap (90 % of it), shown below. Unit test:
  `pipeline/tests/test_resources.py`.

  | memory cap | `n` |
  |---|---|
  | 8 GB | 70 |
  | 16 GB | 300 |
  | 32 GB | 750 |
- **Result:** run 3 with `n = 70` passed; the sampled peak was 6.1 GB, and the two files took
  23.4 + ~6 min.
- **Cost:** two failed full runs, about 2 h 40 min of wall time. The guess (`n = 200`) cost an
  85-minute run. Measuring took 5 minutes.
- **Scale-up note:**
  - **addSpecies needs about 5 GB just to start**, whatever the study size. A 4 GB machine
    (`e2-medium`) can't run it.
  - Its time grows with ASVs ÷ `n` × ~45 s, so on a big study, more memory (bigger `n`) is what
    makes it faster.
  - Rough rule: 8 GB cap ≈ 0.6 min per 100 ASVs; 32 GB cap ≈ 0.1 min per 100 ASVs.
  - A newer SILVA species file with more references raises both terms. Recount `SPECIES_REFS`
    in `run_study.py`.
- **Evidence:** `DATA_DIR/pipeline/23013615/main/ampliseq.stdout.log` (and `.nextflow.log.1`),
  `studies/23013615/resource_profile.json` (runs 1–2, run times only), `pipeline/README.md`.

### E6. `-resume` silently reran everything (2026-11-15, 23013615; affects all single-end studies)

- **Symptom:** resuming after E5 should have rerun one task. It reran all 784, which took
  70 min to get back to addSpecies. Nextflow gave no warning.
- **Cause:** ampliseq 3.1.0's `RENAME_RAW_DATA_FILES` runs `touch <run>.fastq.gz` when a
  single-end file already has that name. `touch` follows the symlink and changes the raw FASTQ's
  mtime. Nextflow's default cache key includes the mtime.
- **How it was found:** two runs of the same task had the same script and inputs but different
  hashes. The FASTQ mtime equalled the time of the second run.
- **Fix:** `process.cache = 'lenient'` (path + size) in `pipeline/conf/local_mac.config`. This is
  safe because every download is md5-checked. Paired-end studies were not affected (`_1`/`_2`
  names are never touched).
- **Cost:** about 70 min (run 2), plus about 70 min for run 3: the cache mode change makes every
  earlier hash invalid one last time.
- **Scale-up note:** on Spot/preemptible VMs, `-resume` is the whole recovery strategy (see
  `gcp-migration.md`). Without this fix, every preemption of a single-end study would restart it
  from scratch.

### E7. Nextflow records no memory on macOS (2026-11-15, all)

- **Symptom:** in the execution trace, `peak_rss` and `%cpu` are `-` for every task.
  Nextflow's macOS task wrapper (`nxf_trace_mac`) records only the run time.
- **Fix:** `pipeline/bin/resource_monitor.py`.
  - While Nextflow runs, it samples `ps` every 5 s and records per-task peak RSS and CPU (the
    tree under each child of the java process, matched to the trace by work-dir hash). It also
    records the peak for the whole pipeline.
  - After the run, the samples are joined with the trace into `studies/<PMID>/resource_profile.json`.
  - On Linux, the trace's own `peak_rss` is used instead.
  - Sampled numbers are lower bounds: a peak shorter than 5 s can be missed.
- **First data:** the run times of the two failed 23013615 runs show where the hours go, in
  steps that run as a single task:
  - DADA2_DENOISING: 19 min;
  - DADA2_QUALITY1: 12.5 min;
  - DADA2_QUALITY2: 7.6 min;
  - DADA2_TAXONOMY: 5.5 min per file.
- **Scale-up note:** more CPUs speed up the per-run steps (FASTQC, filtering), not these single
  tasks. A single study's wall time is set mostly by them.

### E8. Installing tools under the agent's permission check (2026-11-15)

- **Symptom:** the auto-mode permission check blocked `brew install sratoolkit`. The owner's
  own attempt stopped at a y/n prompt.
- **Fix:** sra-tools in a project conda env (`DATA_DIR/conda/sra-tools`, bioconda).
  `sra_source.py` finds it there when `fastq-dump` is not on PATH.
- **Scale-up note:** on a cloud VM, install tools in the startup script or the image, not by
  hand.

Entries E9–E16 were written on 2026-11-16 from the run logs, the resource profiles, project
memory and `gcp-migration.md` section 6, after the GCE batch had finished. They were missed on the day.

### E9. `resource_monitor` crashed on Python 3.12 (2026-11-15, 35756051 GCE smoke)

- **Symptom:** the first GCE attempt crashed right after the download step with
  `TypeError: 'Event' object is not callable`, inside our own wrapper.
- **Cause:** the sampler thread stored its stop flag in `self._stop`, which shadows a private
  method of `threading.Thread`. Python 3.12's `Thread.join()` calls it; the Mac's 3.9.6 never does.
- **Cost:** one VM attempt, a few minutes (estimated).
- **Fix:** renamed to `_stop_event`; a unit test runs start/stop/join on 3.9 and 3.12.
- **Scale-up note:** every new machine image brings its own Python. Run a 6-run smoke test on a
  new platform before a full run, and compare results (demo story 3: counts and ASVs identical
  Mac vs GCE).
- **Evidence:** `docs/demo-stories.md` story 3, `pipeline/tests/`.

### E10. ENA lists a file and its md5 but cannot serve it (2026-11-15, 35756051)

- **Symptom:** fetchngs failed after 27 min: `SRR15400852_2.fastq.gz` md5 mismatch. The download
  was an 802-byte HTML page; ENA's file report and FTP listing looked normal.
- **Cause:** a broken file on ENA's side (HTTPS redirect to a "directory", 403 from the Mac,
  "does not exist" over FTP). Retries gave the same result.
- **Cost:** 27 min, then the study waited until the fix the next morning (measured from the log).
- **Fix:** per-run fallback. When fetchngs gives up on a run, the other runs are fetched again with
  `-resume` and that run comes from NCBI's normalized `.sra` (md5-checked, `fastq-dump --split-3`).
  At most max(3, 5 %) of a study's runs may switch source. Rerun 2026-11-16: the bad run came from
  NCBI in 4 s and all 89 runs were in the samplesheet.
- **Scale-up note:** one bad file must not stop a study. The fallback needs sra-tools on the
  machine that runs `run_study.py` (installed by `infra/startup.sh`; sra-tools 3.4.1 on the Mac and
  3.0.3 on the VM gave identical files).
- **Evidence:** `docs/demo-stories.md` story 4, `fastq_fallback.json`, `studies/35756051/log.jsonl`.

### E11. Pooled DADA2 denoising takes hours on 3 of 8 vCPUs (2026-11-15/16, 37661264)

- **Symptom:** the overnight GCE batch ran about 10 h where 2–5 h had been estimated.
- **Cause:** ampliseq 3.1.0 defaults to `sample_inference = "pooled"`: all samples are denoised as
  one job, whose cost grows much faster than linearly with the number of unique sequences.
  37661264 (97 runs, MiSeq 2×250, 5.4 M reads, 1.08 M unique R1 sequences) spent about
  1 h 45 min per read direction in DADA2_DENOISING on an e2-standard-8, using about 3 of 8 vCPUs.
  For comparison, the whole ampliseq run of 29335008 (38 runs, also pooled) took 36 min.
- **Fix:** owner's decision 2026-11-15: `sample_inference = pseudo` in preflight's FIXED_PARAMS for
  every new study. 23013615, 29335008 and 37661264 stay pooled (not rerun).
- **Measured after the change (pseudo, e2-standard-8):** 35756051 (89 runs) 16 min;
  28174747 (90 runs, HiSeq 1×101) **76 min**. Pseudo is not always fast.
- **Scale-up note:** denoising time follows unique sequences, not the number of runs. Before a
  full run, estimate it from the manifest's read counts and say it up front. Pooled denoising leaves
  most cores idle; run a second study beside it when memory allows.
- **Evidence:** `studies/*/resource_profile.json`, `studies/*/pipeline_run.json` (`sample_inference`).

### E12. A fixed `memory` for an nf-core step turns off its retry (2026-11-16, 37661264)

- **Symptom:** every DADA2_ADDSPECIES chunk was killed 3 times (exit 137,
  `Killed /usr/bin/env Rscript`); the run 03:14–03:43 failed.
- **Cause:** `resources.config` gave the step a fixed `memory = 10.GB`. nf-core steps ask for more
  memory on each retry (`task.attempt`); a fixed value replaces that, so all three attempts got the
  same 10 GB. 2000 mixed-length V3-V4 ASVs per chunk need about 15 GB.
- **Cost:** 29 min run (measured).
- **Fix:** `memory = { [X.GB * task.attempt, CAP].min() }` (`run_study.resource_config`).
- **Scale-up note:** never give an nf-core process a fixed memory value. Memory formulas measured
  on one study type (E5: 454, one ASV length) did not transfer to another (V3-V4, mixed lengths).
- **Evidence:** `studies/37661264/log.jsonl`, `studies/37661264/resource_profile.json`.

### E13. Code pushed to the VM did not reach a running `run_study.py` (2026-11-16, 28174747)

- **Symptom:** after the E12 fix was copied to the VM (03:48), 28174747, whose `run_study.py` had
  started at 03:43, still wrote the old fixed-10 GB `resources.config`. Its ampliseq run
  (04:10–06:23) failed 8 addSpecies attempts.
- **Cause:** Python had already loaded the old module; `run_studies.sh` runs studies one after
  another inside one shell session.
- **Cost:** a 2 h 13 min run failed. The rerun (12:37–12:56) reused 284 finished tasks, so the
  lasting loss was the failed addSpecies attempts plus the wait (measured from the profile).
- **Fix:** after pushing a fix, check the generated `resources.config` of the next study, and
  restart (or queue `--from <stage>`) a study already in progress.
- **Scale-up note:** run each study from a fixed code snapshot (for example one Batch job per study
  with its own copy of the code), so a fix means a new job, not a hot-patched one.
- **Evidence:** `studies/28174747/log.jsonl`, `studies/28174747/resource_profile.json`.

### E14. addSpecies chunks can only run one at a time on 32 GB (2026-11-16, 37661264)

- **Symptom:** the successful 37661264 run took 4 h 57 min (07:40–12:37), nearly all of it in
  DADA2_ADDSPECIES.
- **Cause:** 11 single-threaded chunks of mixed-length ASVs, peak 15–17 GB each (17.1 GB measured),
  5.7 task-hours in total. On 32 GB only one chunk fits at a time.
- **Cost:** about 5 h of VM time for one study. The whole GCE batch (4 studies) used about
  16.5 VM-hours, ~$4.5 of trial credit, mostly in this step and in pooled denoising (E11).
- **Fix:** owner's decision 2026-11-16: `skip_dada_addspecies = true` for every new study; genus
  level is enough for disease vs healthy. The five processed studies keep their species columns.
- **Scale-up note:** if species are ever needed, give each chunk its own ≥ 32 GB machine (for
  example a Batch job per chunk) or use a 64 GB machine; don't run it on a shared 32 GB VM.
- **Evidence:** `python3 -I pipeline/bin/resource_monitor.py report`, `progress/plan.json` decisions.

### E15. The QC length check flags 1×101 reads as wrong-length (2026-11-16, 28174747)

- **Symptom:** verdict `review` for a healthy study: 3617 of 3651 ASVs are 101 nt, and the check
  expects the full ~253 nt V4 amplicon (−40/+20 nt).
- **Cause:** the rule was written from paired-end studies; single-end reads shorter than the
  amplicon give ASVs of read length.
- **Cost:** no rerun; one false `review` that needs a person.
- **Fix:** not yet. The check should use the read length when reads cannot span the amplicon
  (in `needs_you`).
- **Evidence:** `studies/28174747/pipeline_qc.json`, `docs/demo-stories.md` (other cases).

### E16. Whole-pipeline memory peak is undercounted under Docker (2026-11-16, all GCE runs)

- **Symptom:** `resource_profile.json` gives 35756051 a whole-pipeline peak of 1.94 GB, while its
  DADA2_TAXONOMY task alone peaked at 9.6 GB.
- **Cause:** the `ps` sampler follows the process tree under Nextflow's java process. Under Docker
  the task runs in a container started by the Docker daemon, so the tree holds only the `docker`
  client. Per-task peaks are right: on Linux they come from the Nextflow trace (`peak_rss`).
- **Cost:** none yet; it would have misled machine sizing.
- **Fix:** for sizing, use the per-task peaks. To get a whole-machine peak on a cloud VM, sample the
  machine's used memory (`free`) next to the run.
- **Evidence:** `python3 -I pipeline/bin/resource_monitor.py report` (peak total vs per-step peak).

---

## Resource numbers so far

| Study | Runs | Platform | Download | Step | Measured |
|---|---|---|---|---|---|
| 35756051 (smoke) | 6 | MiSeq 2×301 | 0.16 GB | whole ampliseq | 3 attempts: 17 min (incl. building conda envs) until a failure, then a 27 s `-resume` (paired-end, so the cache worked) |
| 23013615 (smoke 2) | 6 | 454 | small | whole ampliseq | 6.5 min |
| 23013615 | 256 | 454 | 1.58 GB .sra (2.5 min) | whole ampliseq, run 3 | **1 h 48 min**, 788 tasks, peak 6.2 GB for the whole pipeline |
| 29335008 (GCE) | 38 | MiSeq 2×301, pooled | | whole ampliseq | 36 min, 181 tasks; denoising 3.6 min; TAXONOMY 9.6 GB |
| 35756051 (GCE) | 89 | MiSeq 2×301, pseudo | 2.19 GB FASTQ | whole ampliseq | 1 h 13 min, 380 tasks; denoising 16 min; TAXONOMY 9.6 GB; addSpecies 9 min |
| 28174747 (GCE) | 90 | HiSeq 1×101, pseudo | 2.43 GB FASTQ | denoising | 76 min (one task), from the failed run (E13) |
| 37661264 (GCE) | 97 | MiSeq 2×250, pooled | | denoising; addSpecies | ~1 h 45 min per read direction (E11); addSpecies 5.7 task-hours (E14) |

GCE rows: e2-standard-8 (8 vCPU, 32 GB), Docker profile, 2026-11-15/16. Whole-pipeline peaks
under Docker are not valid (E16); per-step peaks come from the Nextflow trace. Without addSpecies,
the largest single task in these studies is DADA2_TAXONOMY at about 9.6 GB.

### 23013615 full run, per step (run 3, 2026-11-15, sampled every 5 s)

Measured on the M4 Mac mini (10 cores, 16 GB) with caps of 8 CPUs and 8 GB. Input: 256 single-end 454
runs, ~5,000 reads each (1.3 M reads), 2,544 ASVs of 280 nt. Peaks are lower bounds (sampling).

| Step | Tasks | Peak RSS | Peak CPU | Longest task | Notes |
|---|---|---|---|---|---|
| DADA2_ADDSPECIES | 2 | 6.1 GB | 1 core | 23.4 min | `n = 70`; the E5 model predicts ≤ 7.4 GB. The longest step |
| DADA2_DENOISING | 1 | 2.5 GB | 7.8 cores | 21.9 min | multithreaded, one task for the whole study |
| DADA2_QUALITY1 | 1 | 2.9 GB | 1 core | 14.2 min | single-threaded quality plots; could be skipped |
| DADA2_QUALITY2 | 1 | 2.4 GB | 1 core | 9.2 min | single-threaded |
| DADA2_TAXONOMY | 2 | 6.2 GB | 7.8 cores | 5.9 min | assignTaxonomy, SILVA |
| DADA2_ERR | 1 | 1.8 GB | 4.6 cores | 1.1 min | |
| DADA2_FILTNTRIM | 256 | 0.9 GB | 1 core | 8 s | 0.5 CPU-h in total |
| FASTQC | 256 | 0.3 GB | 1.8 cores | 4 s | |
| whole pipeline | 788 | **6.2 GB** | 7.8 cores | 1 h 48 min | the 8 GB cap was never reached |

**Sizing notes from this run:**
- **Memory:** 8 GB is enough for a 454-size study (with `n = 70`). The largest steps
  (TAXONOMY, ADDSPECIES) hold the SILVA references, so their memory grows with the reference,
  not with the number of runs. More memory mainly buys a larger addSpecies `n`.
- **CPU:** about 70 of the 108 minutes are in four steps that each run as a single task:
  ADDSPECIES, DENOISING, QUALITY1 and QUALITY2. Only DENOISING and TAXONOMY use more than one
  core. On an `e2-standard-8` (8 vCPU, 32 GB), expect a similar wall time with `n ≈ 750`
  (addSpecies would take minutes, not 23). More vCPUs would not help much.
- **Fast wins before buying a bigger machine:**
  1. Skip the QUALITY1/2 plots: about 23 min of single-threaded work, and MultiQC covers QC.
  2. Raise addSpecies `n` where there is memory.
- **To do:** a MiSeq paired-end study (35756051, 37661264) will have more reads per run and
  merging, so its profile will differ. Record it when the first full paired-end run happens.
