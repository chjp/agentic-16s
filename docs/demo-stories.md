# Demo stories

Real cases where the pipeline found a problem in public data. These are raw material for a job
talk or a LinkedIn post about data heterogeneity: how an end-to-end pipeline, plus agentic
checks, catches problems in production that no error message reports.

Rules for this file: describe only what really happened, with the evidence and where it is.
Don't fix the data to make a story; the pipeline's normal behaviour is the story.

---

## 1. A 454 study whose archived reads lost their quality scores (PMID 23013615)

**Status:** resolved by the pipeline on 2026-11-15, with no special handling. The owner had decided
the study goes through the production pipeline like any other. Preflight stopped it, then a
general rule found the real scores at NCBI. All 256 16S runs are now in the database.

**What the study is.** IBD stool 16S, 454 GS FLX Titanium, single-end, V3-V5 (357F/926R).
It has 257 16S runs, the largest of the five curated studies (573 runs in all).

**What preflight measured** (first 2,000 reads of 8 runs, streamed from ENA, a few MB):
- Only **2 distinct quality values** in the FASTQ (`?` and `$`, Phred 30 and 3). The archive has
  replaced the real scores.
- No primers at the read starts (0 % for both), so they were already trimmed.
- 87 % of reads point in the reverse direction (sequenced from 926R), based on conserved 16S
  sites rather than primers.
- 6.8 % of reads span the whole 551 nt amplicon.

**Why it matters.** DADA2 learns its error model as a function of the quality score. With
2 values it has nothing to learn from. No tool would have reported this:
- best case, DADA2 prints an obscure warning while fitting the error model;
- worst case, everything runs green and an ASV table that looks normal goes into the database.

There is no crash and no error message. The failure is silent.

**What the pipeline did.** `preflight.py` returned `needs_review`:

> quality scores carry no information (2 distinct values; the archive replaced them), so DADA2's
> error model cannot use them

`run_study.py` stops there, before the download: no GB fetched, no compute spent, nothing loaded.

**Evidence:**
- `DATA_DIR/pipeline/23013615/main/preflight.json` and `params.json` (`observed.distinct_quality_values = 2`)
- `studies/23013615/log.jsonl` (the preflight entry)
- `pipeline/README.md`, section "What it found in the five curated IBD studies"

**What happened next (the better half of the story).**
- **The quality scores were not gone everywhere.** NCBI's locator lists three copies of each run:
  - the SRA Lite file, flagged `noqual`, which is the one ENA's FASTQ matches;
  - the full "normalized" .sra, public on AWS Open Data;
  - the original SFF.
- **So preflight got a general rule.** When the qualities are lost, it fetches the sampled runs'
  normalized .sra and checks them: SRR578443 has 35 quality values instead of 2, and its
  sequences are identical to ENA's for all 4,950 reads. If the check passes, preflight switches
  the whole study's download source. If no copy has real scores, the study stays at
  `needs_review` as before.
- **454 needed one more fix: one fixed read length.** The first smoke run had
  `truncLen = 0`, so one organism gave many ASVs of different lengths, and only 26 % of reads
  sat in ASVs of plausible length. Preflight now sets a fixed `trunclenf`; this study gets 280.
- **The full run then failed twice on memory (story 2) before it finished.**

**Result (full run, 2026-11-15):**
- Download: 256 runs, 1.58 GB of .sra (md5-checked).
- ASVs: 2,544, all 280 nt; 1.4 % of reads unassigned at phylum level.
- QC: 229 runs ok, 27 below 1,000 reads. Verdict **review**: 4 runs keep less than 20 % of their
  reads in quality filtering. Flagged, not hidden.
- Database: loaded into `bio.*` (pipeline_run 3). Per run, `reads_input` equals the archive
  `spots` and the abundance sum equals the non-chimeric reads (0 mismatches). Then the FASTQ was
  deleted and `core.run` set to `deleted`.
- Biology sanity check, straight from SQL: mean Faecalibacterium share is 7.6 % in CD (108 runs),
  9.8 % in healthy controls (24) and 10.1 % in UC (69). The lower share in CD is the expected
  direction.

**Evidence (added):** `pipeline/bin/sra_source.py`, `studies/23013615/sources/archive/ncbi_sdl_*.json`,
`studies/23013615/pipeline_qc.json`, `studies/23013615/resource_profile.json`, `bio.pipeline_run`
row 3 (`params._fastq_source = ncbi_sra_normalized`).

**Options that were considered:**
- Look for the original submitted SFF files. ENA has no `submitted_ftp` for these runs; NCBI has
  them but needs "rehydration".
- Use a denoiser that doesn't use quality scores (for example Deblur).
- Leave the study out.

**Talking points:**
- Heterogeneity isn't only metadata. The *reads themselves* differ across five studies: two primer
  sets, three regions, primers kept or trimmed, forward or reverse orientation, and real or
  replaced quality scores.
- The dangerous failures in production are silent ones. Crashes are the easy case.
- The fix is to measure the data against what the paper says it should be (preflight, QC gate),
  not to read logs after the fact.
- Stopping was the right first move, and it isn't the end of the story. A stop with a precise reason
  ("2 distinct quality values") pointed straight at the fix: look for another copy of the data.
- Next step (see the schema open question Q10): put every such finding in an issue table with
  who detected it and who classified it. Then "how many studies stall, and why" becomes a SQL
  query.

---

## 2. Fixing one problem moved the failure downstream (23013615 full run, 2026-11-15)

**What happened.** Fixing story 1 took two steps. First, fetch the real quality scores from NCBI.
Second, truncate every 454 read to one fixed length, so that one organism gives one ASV and not
one ASV for every read length. The 6-run smoke test passed. The full run (256 runs) crashed after
76 minutes, at the second-to-last step:

> DADA2_ADDSPECIES: Error: vector memory limit of 16.0 Gb reached

**Why.** dada2's species step compares every ASV *of the same length* against all 1.47 million
SILVA species references at once. Before the fix, the ASVs had many lengths, so the work came in
small pieces. After the fix, all 2,000 ASVs in a file had the same length (280 nt), so they made
one huge piece. The smoke test had too few ASVs to show it. The fix for the data (one length per
ASV) changed the shape of the compute.

**The second failure: a guess.** My first fix was an estimate on paper: smaller chunks
(`n = 200`). That run crashed at the same step 85 minutes later. Then I measured, using two short
runs on 50 and 100 ASVs (a few minutes):
- a fixed ~5 GB for the references and their reverse complement;
- plus 34 MB per ASV in a chunk.

At `n = 200` that is about 12 GB, plus R's garbage-collection headroom, which reaches 16 GB. The
chunk size is now computed from the measured cost and the machine's memory cap (`n = 70` at
8 GB). There is a unit test for that.

**The third failure: hidden.** The rerun with `-resume` should have taken minutes. It took
70 minutes, because Nextflow reran every task. Nextflow gave no warning; the cause showed only in
a file timestamp:
- ampliseq's rename step runs `touch` on single-end files that already have the expected name;
- `touch` goes through the symlink and changes the raw FASTQ's mtime;
- Nextflow's cache key includes the mtime, so every cached task looked new.

The fix: Nextflow's `lenient` cache key (path + size). It is safe because every download is
md5-checked. The same problem would have hit every single-end study.

**Evidence:** `DATA_DIR/pipeline/23013615/main/ampliseq.stdout.log` (and `.nextflow.log.1`),
`studies/23013615/log.jsonl` (two `run nf-core/ampliseq` failures), `pipeline/bin/run_study.py`
(`addspecies_chunk`), `pipeline/conf/local_mac.config` (`process.cache`), `pipeline/README.md`.

**Talking points:**
- Smoke tests check the logic, not the scale. A step that grows with the data (here, ASVs of one
  length × reference size) needs either a measured cost model or a run on full-size data.
- Measure before you tune. A guessed parameter cost an 85-minute run. Measuring took 5 minutes.
- Some failures are silent: here the cache missed and nothing reported it. The cost showed up
  only as wall-clock time.
- Heterogeneity reaches the infrastructure. Single-end and paired-end inputs take different
  code paths in the same nf-core pipeline, and only one of them breaks caching.

---

## 3. Same pipeline, two machines: a hidden Python bug, then identical results (35756051 smoke, 2026-11-15)

**What happened.** The 6-run smoke test of 35756051 ran first on the Mac (Apple silicon, conda
environments, system Python 3.9.6), then on a Google Cloud VM (e2-standard-8, Ubuntu 24.04,
nf-core's Docker containers, Python 3.12). Same code, same pinned versions (Nextflow 26.10.6,
ampliseq 3.1.0, SILVA 144, seed 100).

The first VM attempt crashed right after the download step, inside our own wrapper code:

> TypeError: 'Event' object is not callable

**Why.** The resource sampler (`pipeline/bin/resource_monitor.py`) is a `threading.Thread` subclass
that stored its stop flag in `self._stop`. That name shadows a private method of `Thread`. Python
3.9 never calls it; Python 3.12's `Thread.join()` does. The Mac had run this code for days without
a hint. Fix: rename the attribute to `_stop_event`, and a unit test now runs start/stop/join
(passes on 3.9 and 3.12).

**Then the comparison** (`studies/35756051/pipeline_qc_subset6.json` vs `pipeline_qc_subset6_gce.json`;
tables under `DATA_DIR/pipeline/35756051/main_subset6/tables` and `DATA_DIR/gce/35756051/main_subset6/tables`):

| | Mac (conda) | GCE (Docker) |
|---|---|---|
| Runs, read counts at every step (input → non-chimeric) | 6 runs | identical for all 6 runs |
| ASVs | 389 | 389, same sequences (md5) |
| Abundance table | 860 rows | identical, every cell |
| Median merge rate / retention | 0.98 / 0.472 | same |
| Top genera, QC verdict | Bacteroides 0.453 …, `review` (SRR15400843 keeps 3 %) | same |
| Taxonomy | | 2 of 389 labels differ, 7 bootstrap confidences differ |

The denoising (cutadapt, DADA2) is bit-identical across operating systems, CPU architectures and
packaging (conda vs containers). The only differences are in the naive Bayes classifier's bootstrap:
7 rare ASVs (1 to 69 reads, at most 0.03 % of reads) whose confidence sits next to the 0.5 cut-off,
for example a Lachnospiraceae ASV at genus confidence 0.49 on the Mac (left unassigned) and 0.50 on
the VM (Roseburia). The seed is pinned, but the classifier's random draws are not reproducible across
platforms. One software version differs, on purpose: the Mac's Cutadapt summary step uses Python
3.8.20 because 3.8.3 has no osx-arm64 build (`pipeline/conf/ampliseq_osx_arm64.config`).

**Takeaways for the talk.**
- Running the same smoke test on a second platform found a real bug the first platform could never show.
- "Reproducible" has levels: counts and ASVs reproduce exactly; low-confidence taxonomy calls near
  a threshold do not, even with a fixed seed. Analyses should not hinge on single rare ASVs at genus level.
- Cost of the check: about 30 minutes of an 8-vCPU VM (~$0.14, paid by free-trial credit).

---

## 4. ENA lists a file, publishes its md5, and cannot serve it (35756051 full run, 2026-11-15)

**What happened.** The full run of 35756051 (89 runs) on the GCE worker stopped in the download step
after 27 minutes:

> SRX11703076_SRR15400852_2.fastq.gz: FAILED | md5sum: WARNING: 1 computed checksum did NOT match

**What we checked** (2026-11-15, 21:40–21:45 UTC, from the VM and from the Mac):
- ENA's file report still lists the run with two files, sizes `5742076;8348976` and md5
  `8951fa2f…;d7a022cf…`. The FTP directory listing shows both `SRR15400852_1.fastq.gz` and `_2.fastq.gz`.
- Read 1 downloads fine and its md5 matches.
- Read 2 cannot be downloaded: over HTTPS the server answers with a 291-byte HTML redirect to
  `…/SRR15400852_2.fastq.gz/` (as if it were a directory), from the Mac it answers 403, and over FTP
  curl says "The file does not exist". The nf-core download got an 802-byte HTML page instead of the
  8.3 MB gzip file. Two retries gave the same result, so it is not a one-off network error.
- NCBI still has the full run (`SRR15400852.sra`, 16.7 MB, md5 `3f355c33…`).

**Why it matters.** Nothing in ENA's metadata says the file is broken; the listing, the size and the
md5 all look normal. Without the md5 check the pipeline would have tried to read an HTML page as
FASTQ, or silently lost read 2 for one sample. The check turned it into a clear, attributable failure.
The cost: one bad file stopped the whole study's download (the other 88 runs had to wait).

**What we changed** (general rule, not a one-run fix): when fetchngs gives up on a run's ENA file, that
run is dropped from the ENA download, the other runs are fetched again with `-resume` (finished downloads
are reused), and the dropped run comes from NCBI's normalized `.sra` instead: md5-checked, dumped with
`fastq-dump --split-3` in ENA's read-name format (`run_study.fetch_ena_fallback`, `sra_source.fetch_split`).
At most max(3, 5 %) of a study's runs may switch source; more failures stop the study, because that
looks like an outage rather than a broken file. Each switched run is listed with its reason in
`fastq_fallback.json`, `pipeline_run.json` (`fastq_source_by_run`) and the activity log.
Checked before relying on it: NCBI's read 1 for SRR15400852 is identical to ENA's read 1 after
decompression, and both dumped files have exactly ENA's listed byte sizes (sra-tools 3.4.1 on the Mac
and 3.0.3 on the VM give identical output).

**Result** (2026-11-16 06:26–06:28 UTC on the GCE worker): fetchngs failed on SRR15400852 again, the
fallback took that run from NCBI in 4 seconds, and the samplesheet had all 89 runs.

---

## Other cases worth telling (smaller)

- **37661264:** the paper gives no primers. Preflight matched a catalog pair (341F Novogene /
  806R) in 86 % / 95 % of reads, counting 5'-clipped primers. Without that, only about 42 %
  matched.
- **28174747:** EMP-style HiSeq reads start right after 515F, so there is nothing to trim
  (`skip_cutadapt`).
- **35756051 smoke run:** one run (SRR15400843) kept 3 % of its reads in quality filtering. The
  QC gate flagged it for review. The data were kept, not hidden.
- **35756051 full run (2026-11-16):** the smoke run's one bad sample was not an exception. In the full
  study, 42 of 89 runs keep under 20 % of their reads in quality filtering (several 0 %), median 38 %.
  Six runs chosen for the smoke test could not show this. Verdict `review`; not tuned away.
- **28174747 full run (2026-11-16):** the QC gate's length check flagged a healthy study: HiSeq 1×101
  single-end reads give 101 nt ASVs, while the check expects the full ~253 nt V4 amplicon. A QC rule
  written from paired-end studies misfired on a different read layout; the check needs to know the
  read length, not just the amplicon.
