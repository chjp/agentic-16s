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
| E17 | 2026-11-16 | infra | the project's first Google Batch job failed in 8 s with no reason given | one resubmit, ~5 min |
| E18 | 2026-11-16 | infra | Batch's Debian 12 image: `apt-cache show` finds openjdk-21, `apt-get` cannot install it | one probe job (10 s of VM) |
| E19 | 2026-11-16 | infra | on Batch, `run_study.py` could not find `nextflow` that the shell had just run | one smoke job (2 min of VM) |
| E20 | 2026-11-16 | infra | one dropped connection while pulling the DADA2 image stopped a whole Batch run (exit 125, no retry) | 38.5 min of VM, ~$0.18 |
| E21 | 2026-11-16 | ops | with gcloud's CRC32C helper off, a 72 MB download was silently skipped | one failed collect |

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
- **Update 2026-11-16 (measured on Google Batch):** the per-task trace peak is not enough either.
  The 35756051 6-run smoke on Batch (e2-standard-8, `vm_memory.tsv`, `free` every 10 s) shows the
  whole VM at 26.9 GB of 32 GB used during DADA2_TAXONOMY. The climb from 2.4 GB starts when the
  task starts, lasts about 70 s and falls back to 7 GB, then the task holds about 12 GB for 3 min.
  The Nextflow trace gives that task `peak_rss` 9.6 GB. Nothing else was running: every other task
  stayed under 2 GB and the VM never went above 6.5 GB before taxonomy. Likely cause, not
  checked: a short peak while SILVA is loaded, which the trace's sampling misses. Consequence:
  keep 32 GB for any study that assigns taxonomy with SILVA; do not drop to 16 GB (e2-standard-4)
  on the strength of the 9.6 GB figure. Evidence:
  `~/agentic16s-data/batch/runs/a16s-35756051-20261117-003156/output/vm_memory.tsv` and that run's
  `execution_trace_*.txt`.

### E17. The first Google Batch job failed with no reason given (2026-11-16, probe job)

- **Symptom:** the probe job `a16s-probe-20261117-001625` went from QUEUED to FAILED 8 s after
  submission. `runDuration` was 0 s, no VM was created, and the job's status events give no
  reason.
- **Cause:** Batch creates its service agent (`service-<project number>@gcp-sa-cloudbatch`) on
  first use, not when the API is enabled. The audit log shows `service-agent-manager` adding
  `roles/batch.serviceAgent` at 00:16:32 UTC; the job failed at 00:16:33, before the grant had
  taken effect.
- **Cost:** about 5 min to find the cause in the audit log, then one resubmit. No VM time.
- **Fix:** resubmit a few minutes later. In a new project, enabling the API is not enough: submit a
  small probe job first and expect the very first one to fail like this.
- **Scale-up note:** Batch reports this failure as a bare state change, so check the audit log
  (`logName:"cloudaudit"`, last few minutes) before anything else. The job VM's own logs only exist
  once a VM has started.
- **Evidence:** `~/agentic16s-data/batch/runs/a16s-probe-20261117-001625/` (submit.json,
  job.json), Cloud Logging audit entries at 00:16:32–33 UTC on 2026-11-17.

### E18. `apt-cache show` finds a Java package that cannot be installed (2026-11-16, probe job)

- **Symptom:** the second probe job (`a16s-probe-20261117-001805`) stopped 10 s after starting:
  `E: Package 'openjdk-21-jre-headless' has no installation candidate`.
- **Cause:** Batch's `batch-debian` image is Debian 12 (bookworm), which ships Java 17 only. The
  runner picked Java 21 when `apt-cache show openjdk-21-jre-headless` succeeded, and that command
  also succeeds for a name that another package merely refers to. The GCE worker ran Ubuntu 24.04,
  where Java 21 installs, so the problem never showed there.
- **Cost:** one probe job, 10 s of an e2-standard-2 (well under $0.01).
- **Fix:** ask `apt-cache policy` for a real candidate (`Candidate: <version>`) and fall back to
  `openjdk-17-jre-headless`.
- **Scale-up note:** the cloud image is a different OS from the VM that was tested: check every
  package choice on it with a short probe job before running a study.
- **Evidence:** `~/agentic16s-data/batch/runs/a16s-probe-20261117-001805/output/runner.log`.

### E19. Python could not find `nextflow` on a Batch VM, the shell could (2026-11-16, 35756051 smoke)

- **Symptom:** the first Batch smoke job (`a16s-35756051-20261117-002457`) passed preflight and
  stopped at fetch, 2 min after starting: `FileNotFoundError: [Errno 2] No such file or directory:
  'nextflow'` from `subprocess.Popen` in `run_study.py`. The probe job had run `nextflow -version`
  from the same script without trouble.
- **Cause (likely; the next job logs whether Batch exports PATH):** Batch starts the script
  without an exported `PATH`. bash then uses its built-in default, which includes
  `/usr/local/bin`, but does not export it. Python's `subprocess` falls back to `/bin:/usr/bin`,
  which has `docker`, `java` and `python3` but not `/usr/local/bin/nextflow`. On the GCE worker
  every run started from an SSH/tmux login shell with a full PATH.
- **Cost:** one smoke job, 2 min of an e2-standard-8 (~$0.02). The collect step on the Mac also
  crashed, because it tried to compare tables the failed job never wrote.
- **Fix:** the runner exports a full `PATH` first thing and checks, from Python, that `nextflow`
  can be found before starting the study. The probe now prints what Python sees
  (`shutil.which`), not only what bash sees. `collect` compares only when the job produced tables,
  and collecting the same job twice no longer duplicates log lines.
- **Scale-up note:** a probe has to test tools the way the pipeline calls them (here, from a
  Python subprocess). A check from the shell said everything was fine.
- **Evidence:** `~/agentic16s-data/batch/runs/a16s-35756051-20261117-002457/output/runner.log`,
  `studies/35756051/batch_runs.jsonl` (first line).

### E20. One dropped image pull stopped a whole Batch run (2026-11-16, 35756051 full)

- **Symptom:** the full 35756051 job (`a16s-35756051-20261117-005523`, 89 runs) failed 5.5 min
  into ampliseq: `DADA2_FILTNTRIM (SRR15400632)` exit status 125, `docker: failed to copy: read
  tcp ... read: connection reset by peer` while pulling
  `community.wave.seqera.io/library/bioconductor-dada2_r-base_r-digest_tbb`.
- **Cause:** every Batch job starts on a new VM with no images, so each run pulls every container
  again. The GCE worker kept its images between studies and never hit this. Exit 125 means
  `docker run` itself failed. nf-core retries only out-of-memory and similar exit codes, so one
  network error ended the run.
- **Cost:** 38.5 min of e2-standard-8 (~$0.18): fetch took 30 min (2.20 GB from ENA, about
  1.2 MB/s, plus the known NCBI fallback for SRR15400852, E10), then 5.5 min of ampliseq. The work
  dir went with the VM, so a rerun starts from the beginning (the known cost of one job per study).
- **Fix (2026-11-16):** `pipeline/containers.txt` lists the 13 images from every Docker trace so far.
  `infra/batch_runner.sh` pulls them in the background while fetch runs, retrying each failed pull
  up to 5 times (waits 20, 40, 60, 80 s), and logs to `prepull.log`. It runs `run_study.py` in two
  calls, `--to fetch` and then `--from ampliseq`, so ampliseq starts only once the pulls are done.
  Both calls share the job's time limit. A pull error now costs a retry, not the run. Tested on the
  Mac with a fake `docker` (a pull that fails once, then succeeds; one that always fails).
  Verified on Batch (6-run smoke `a16s-35756051-20261117-020003`, 17.4 min): the very same error
  (`connection reset by peer`, on `bash_coreutils`) came up once and the retry pulled it;
  13 images in 167 s, done before fetch ended, so no extra wall time.
- **Scale-up note:** short-lived cloud VMs pull every image on every job, so registry errors hit
  them much more often than a long-lived worker. Pre-pull with retries, or mirror the images to
  Artifact Registry in the same region.
- **Evidence:** `~/agentic16s-data/batch/runs/a16s-35756051-20261117-005523/output/` (runner.log,
  nf_logs.tgz, results.tgz → `ampliseq.stdout.log`), `studies/35756051/batch_runs.jsonl`.

### E21. gcloud skipped a download once its CRC32C helper was off (2026-11-16, Mac)

- **Symptom:** collecting the failed full job left `results.tgz_.gstmp` (72.6 MB) and printed
  "no results.tgz from the job", although the object was in the bucket. Run by hand, gcloud said:
  `This copy was skipped since fast hash calculation tools are not installed`.
- **Cause:** the SDK's `gcloud-crc32c` helper (Homebrew install, ad-hoc signed, quarantined) is
  blocked by macOS Gatekeeper. The owner saw its "Not Opened" dialog, and the first download
  that needed it never finished. Turning the helper off (`storage/use_gcloud_crc32c=false`) left no
  fast CRC32C on the Mac. gcloud downloads large objects in parallel slices, which can only be
  checked with CRC32C, and with the default `check_hashes=if_fast_else_fail` it skips such copies.
  `collect` ran the copy with errors ignored.
- **Cost:** one collect with no results, about 15 min to trace; no data lost (the bucket keeps the
  objects for 30 days).
- **Fix:** `infra/gcloud16s` also turns off sliced downloads and composite uploads, so every
  transfer is a whole object checked with MD5 (hash checks stay on). `collect` now prints gcloud's
  error when a download is incomplete. A second `--collect` of the same job replaces its record
  instead of appending another.
- **Scale-up note:** do not turn off a safety check without testing the largest transfer it
  covers: a small-file test passed here, and the 72 MB file failed.
- **Evidence:** `infra/gcloud16s` (comment), the MD5 of the re-downloaded `results.tgz` matches
  the bucket's.

### E22. Two Batch jobs failed on the same evening for two different reasons (2026-11-17, 37696680 smoke, 36111238 full)

- **Symptom:** two jobs submitted back to back both failed early.
  - 37696680 smoke (`a16s-37696680-20261117-220551`, and again unchanged as `...-222450`): preflight
    crashed with `TypeError: '<' not supported between instances of 'NoneType' and 'float'` in `decide()`.
  - 36111238 full (`a16s-36111238-20261117-221219`): fetchngs stopped because
    `SRA_RUNINFO_TO_FTP (SRR20914746)` wrote no output. Its `SRA_IDS_TO_RUNINFO` had exited 0 with an
    empty runinfo, and `SRA_IDS_TO_RUNINFO (SRR20914760)` had failed once and been retried.
- **Cause, 36111238 (inferred):** an empty answer from ENA's metadata API. The same job resubmitted
  unchanged ran to the end (`a16s-36111238-20261117-223117`, 88.5 min on the VM).
- **Cause, 37696680 (confirmed):** not ENA flakiness. First I blamed ENA because both jobs failed
  within 13 minutes; the unchanged resubmission failed the same way, which ruled that out.
  - ENA's FASTQ for this study is derived from SRA Lite: every quality character is `?`
    (2 distinct values), like the 454 study in E3.
  - preflight then switches to NCBI's normalized .sra, which still has the real scores (30 MB for
    SRR23492472 against 12.5 MB for the Lite copy). But that fallback was built for the single-end 454
    study: `sra_source.fetch` dumps a paired run into one file, so preflight saw no R2, had no reverse
    primer share, and `decide()` compared `None` with a number.
  - On the Mac the same runs read fine, because sra-tools is not installed there and the fallback is
    skipped; the Batch VM has sra-tools.
- **Cost (measured):** 4.9 + 4.1 + 8.0 VM minutes, about $0.10 (estimates from the job records), and
  about 40 min to trace.
- **Fix:**
  - 36111238: resubmitted unchanged.
  - preflight: a mate file that yields no reads is now an error for that run, and a paired protocol
    with no R2 observations goes to `needs_review` instead of crashing (tests in `test_preflight.py`).
  - Owner approved, done the same evening (commit "SRA-Lite fallback: dump paired runs from NCBI as R1
    and R2"): preflight's quality fallback and the fetch stage use `sra_source.fetch_split` for paired
    runs, keeping only `<run>_1/_2` (the unpaired-reads file `<run>.fastq.gz` would sort first).
- **Scale-up note:**
  - A fallback path tested on one study type (454 single-end) is untested for the others; give
    each fallback a test per layout.
  - Two failures close together are not proof of a shared cause: resubmit once, unchanged, before
    concluding "transient".
  - Every external answer needs an "empty is an error" check, not only an "HTTP error is an error"
    check.
- **Evidence:** `~/agentic16s-data/batch/runs/a16s-37696680-20261117-222450/output/runner.log`,
  `.../a16s-36111238-20261117-221219/extracted/data/pipeline/36111238/main/fetchngs.stdout.log`,
  NCBI SDL for SRR23492472 (normalized .sra and the submitted R1/R2 FASTQ are both listed).
- **After the fix (measured, 2026-11-18):** the 37696680 smoke (`a16s-37696680-20261118-050425`,
  26.6 VM min, ~$0.13) passed QC: 6/6 runs from NCBI's normalized copy as R1/R2, primers at the start
  of 99.6 % of R1 and 98.7 % of R2, merge rate 94.7 %, median 50,481 non-chimeric reads.

### E23. A 1-bp shorter sequencing batch lost 98 % of its reads to truncation (2026-11-17, 36111238)

- **Symptom:** the first full run (`a16s-36111238-20261117-223117`, 89.4 min, ~$0.41) passed every
  stage, but the QC gate said `review`: 15 of 60 runs kept under 20 % of their reads in quality
  filtering (SRR20914781 1 %, SRR20914777 1 %, ...).
- **Cause (confirmed):**
  - 46 runs are 2x301 and 14 are 2x300 (a second sequencing batch, AWGB16SAA211). Each read starts
    with an 8-nt spacer, then the 19-nt primer, so R1 of a 2x300 run is 273 nt after cutadapt.
  - ampliseq's automatic truncation follows the majority: `truncLen = c(274, 260)`
    (`ampliseq/dada2/args/filterAndTrim.args.txt`).
  - DADA2 drops every read shorter than truncLen: the 2x300 runs kept a median 2.2 % of their reads
    in filtering, the 2x301 runs 60-66 %.
  - The 14 short runs are 10 of the 20 healthy controls and 4 UC-in-remission, so the loss was
    confounded with the comparison the study is about. The curation agent had already noted the
    300/301 split; nothing in the pipeline used it.
- **Cost (measured):** one full run that cannot be used as it is (89.4 VM min, ~$0.41), and about
  30 min to trace.
- **Fix:** preflight reads every run's length per mate from ENA (base_count / read_count, only runs
  whose reads all have one length), finds the shortest batch (>= 3 runs or 5 %), and caps
  `trunclenf/trunclenr` below what it reaches (largest spacer seen, 2-nt margin), unless that breaks
  merging (then review). 36111238 now gets 271/257 for a 429-nt amplicon. A dry check on the other
  six studies changes nothing. The paper's 338F (one base short of the catalog's) now matches, so
  the merge check has an amplicon length. Rerun from preflight.
- **Scale-up note:** an automatic setting chosen from the majority of a pooled study can silently
  remove a minority batch, and batches often line up with groups (controls sequenced separately).
  Any per-study parameter that discards reads needs a check against the shortest or worst batch,
  not the median run. The QC gate's per-run retention check is what caught it.
- **Evidence:** `studies/36111238/pipeline_qc.json`, `~/agentic16s-data/pipeline/36111238/main/tables/run_qc.tsv`,
  `studies/36111238/sample_sheet.tsv` (`other_json.read_length_per_mate`).
- **Rerun (measured, 2026-11-18):** `a16s-36111238-20261118-034142`, 81.0 VM min, ~$0.37, with
  `trunclenf 271 / trunclenr 257`. The 14 runs of 2x300 kept a median 65.2 % of their reads in
  filtering, the same as the 46 runs of 2x301 (65.2 %; before the fix 2.2 % against 60-66 %).
  Median non-chimeric reads per run: 41,863 (2x300) and 46,814 (2x301). QC verdict `review` only
  for one run, SRR20914791 (17 %, a 2x301 run). Loaded as pipeline_run 10 (60 runs, 2,548 ASVs).
  Within each batch UC still has more Faecalibacterium than the controls (2x301: 10.7 % vs 6.6 %;
  2x300: 9.9 % vs 7.9 %), so that difference is not a batch artifact.

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
| 42349924 (Batch) | 69 | MiSeq 2×301, pseudo | 1.77 GB FASTQ | whole job | 114.6 min, ~$0.52; denoising tasks up to 11 min; TAXONOMY 9.7 GB |
| 36111238 (Batch, rerun) | 60 | MiSeq 2×301 + 2×300, pseudo | 5.5 M read pairs | whole job | 81.0 min, ~$0.37 (fetch 21 min incl. image pull); denoising 23 min; TAXONOMY 9.7 GB |
| 37696680 (Batch) | 53 | MiSeq ~2×250 (trimmed), pseudo | 4.8 M read pairs from NCBI .sra | whole job | 100.0 min, ~$0.46; denoising 22 min; TAXONOMY 9.7 GB |

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
