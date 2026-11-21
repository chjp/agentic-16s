# Heterogeneous 16S data: an agent that measures, on a base of alignment and statistics

Design note, 2026-11-18, revised the same day after the owner's comments. This is design thinking,
not a decision. It draws on the 23 rows of [`failure-taxonomy.md`](failure-taxonomy.md), the
experience log (E1–E23), the QC verdicts of the eight studies run so far,
[`ml-results.md`](ml-results.md), and Chen, Marsh & Al-Hebshi 2022, *SMDI: an index for measuring
subgingival microbial dysbiosis* (J Dent Res 101:331–338). That paper pooled six published studies
through a BLASTn-based, species-level pipeline and built a disease index on top.

**Owner's direction (2026-11-18):**
- Thresholds set per study are fine, as long as an agentic workflow sets them. The workflow is
  what the project is meant to show.
- The goal is not to copy the SMDI pipeline. It is to go back to something more basic, alignment
  plus statistics, that copes with the heterogeneity seen so far, and to fit it into the agentic
  architecture.

## Short answer

1. **Per-study thresholds, set by an agent that measures.** A parameter agent sees the reads only
   through measurement tools. It proposes values, tests them with probes, and submits them to a
   deterministic validator. It never grades its own work. Today's rules stay as its baseline and
   fallback. What does not work is an LLM picking values from a QC summary. Section 2 shows that
   each reason that fails becomes a requirement on the agent.
2. **The basic layer underneath: put every read on one 16S map, then do statistics on what the map
   shows.**
   - Every read gets a position on the E. coli 16S coordinates.
   - A run's error rate is measured from mismatches against reference sequences.
   - Abundance is estimated by likelihood, at the rank the sequenced region can actually resolve.
   - QC flags outliers within a batch and losses that differ between cases and controls.
   - Cross-study effects are contrasts within each study, combined by meta-analysis.
3. **Why this layer.** Ten of the 23 catalog rows (Q05–Q14) exist only because DADA2 needs one fixed
   truncation length. Most of Q04–Q16 become one measurement on the read map. The agent's tools are
   built from the catalog, so the catalog keeps growing.
4. **Why it is an AI-engineering showcase.**
   - The agent's tools come from the catalog, and a deterministic validator checks its output.
   - Every tool call and model response is logged and can be replayed.
   - An evaluation suite injects each catalog problem into clean reads. It scores the rules-only
     baseline against the agent, model by model, for accuracy and cost.

## 1. What the eight studies say

| Observation | Numbers | Source |
|---|---|---|
| Catalog rows by kind | 23 rows: 3 archive delivery (Q02–Q04), 2 quality scores (Q01, Q15), 10 read layout vs what DADA2 needs (Q05–Q14), 8 curation and screening (Q16–Q23) | `failure-taxonomy.md` |
| QC verdicts | 2 `pass`, 6 `review`. In 5 of the 6, runs keep < 20 % of their reads in quality filtering; in the sixth (28174747) the length check misreads 1×101 reads (E15) | `studies/*/pipeline_qc.json` |
| Worst open case | 35756051: median filter retention 38 %, 42 of 89 runs under 20 %, several 0 % (MiSeq 2×301, `trunclenf 281`, `trunclenr 178`). Cause not traced yet; the smoke run's one bad run failed on a poor reverse read | `pipeline_qc.json`, `pipeline/README.md` |
| Small cases | 23013615, 36111238, 37696680, 42349924: 1–4 runs each under 20 %. These may simply be poor samples | `pipeline_qc.json` |
| A cliff, not a slope | 36111238: 14 runs of 2×300 among 2×301 kept a median 2.2 % of reads under the majority's truncation, and 65 % after the cap. Those 14 were mostly controls (E23) | experience log |
| Resolution actually used | 0 ASVs shared between any two studies; the model uses genus CLR | `ml-results.md` |
| Label-aligned offset | 42349924 scores AUROC 0.49 when held out: its controls come from another hospital and BioProject | `progress/plan.json` |

Two things follow.

- The parameters that cause trouble are the ones that **discard reads at a hard edge**. With
  `truncLen`, a read one base too short is dropped whole. With `maxEE` counted over that fixed
  length, a poor read tail sinks the whole read.
- The most dangerous failures are **losses or offsets that line up with the label** (E23's 14
  runs, 42349924's controls). Fixed cutoffs on medians do not see them; a statistical test against
  the label does.

## 2. Who sets the thresholds

| Level | Who decides | Example | Verdict |
|---|---|---|---|
| 0 | Nobody: one setting for all | ampliseq defaults | fails on our data |
| 1 | Rules over observations | today's preflight (primers, orientation, overlap, shortest batch) | the baseline and the fallback |
| 2 | Probes: try candidate values on a sample, keep the best by a stated objective | not built yet | becomes the agent's main tool |
| 3 | An LLM reads a QC summary and picks values | none | no: unmeasured, unrepeatable |
| 4 | **An agent with measurement tools and a validator** | proposed here | the design |
| 5 | An agent at development time turns a new case into a tool, a check and a test | how the catalog grew (Q05 after E23) | keeps the catalog growing |

### What went wrong with level 3 becomes a requirement on level 4

| Lesson | Requirement on the agent |
|---|---|
| A parameter's effect can be measured in seconds on 2000 reads per run | Every non-default value cites a probe result by id. The validator rejects a value without one |
| In E23 the medians looked fine; the loss showed only per run, grouped by batch | Tools return numbers per run and per batch, not medians. The validator checks the worst batch and the loss by label |
| A model call can give a different value next time | Values are frozen in `params.json` with the evidence ids, model id and prompt hash. Reruns read the frozen values and never call the model again. Every response is stored |
| Quiet fixes stop the catalog from growing | When no catalog row matches what it sees, the agent opens a catalog proposal (case, fixture, suggested rule) as a task for a person |
| Parameter choices have no gold set | The evaluation suite of section 5 is the gold set: recorded outcomes of the eight studies plus injected faults with known answers |

This is the same pattern [`proj.md`](../proj.md) already sets for the label-locating agent: the
agent returns a table, plain code checks it against the paper's counts, a mismatch means a retry,
and repeated failure goes to the human queue.

## 3. The basic layer: one map for every read, then statistics

### 3.1 What to take from the SMDI paper, and what to change

The paper classified each read on its own against full-length curated references (BLASTn,
≥ 98 % identity and coverage). It gave ties an honest multi-species label, worked in CLR, and
validated across studies and across regions. Its index (mean CLR of the disease-associated species
minus the health-associated ones) reached AUC 0.92 on three studies it was not trained on. Its
authors note that using the index on new data "requires some standardization of bioinformatic
analysis".

| Keep | Change |
|---|---|
| Each read is placed against full-length references, not cut to one length first | Replace the 98 % identity cliff with a likelihood that uses the measured error rate (3.3) |
| Ties stay honest (multi-species labels) | Report at the rank the region can resolve, computed from the references (3.3) |
| CLR and a simple index | Effects within each study, combined by meta-analysis (3.5) |
| Per-dataset thresholds (they used a length cutoff per region and Q35 or Q30 by depth) | The agent sets them per study, from read-map measurements, through probes and the validator (section 4) |
| Species level (HOMD plus V1–V3 resolves oral species) | Genus groups for gut; species only where the references resolve them |

### 3.2 The read map: where every read sits on the 16S gene

Every 16S read comes from somewhere on a gene of about 1540 nt. Place each sampled read on the
E. coli 16S coordinates (the frame where V4 is about 515–806), and most of the heterogeneity turns
into measured numbers for each run.

| Read-map measurement | Replaces or adds | Catalog rows |
|---|---|---|
| Strand of each read | orientation logic | Q08 |
| Start coordinate, and the bases before it | primer present (read starts at the primer site), already trimmed (starts just after it), spacer (extra bases), clipped primer | Q09–Q12 |
| Primer site from the coordinates, matched to the catalog's `ecoli_start`/`ecoli_end` | primers when the paper prints them wrong or not at all | Q13, Q14 |
| End coordinate and length per run and per mate | amplicon length, expected overlap, read-length batches | Q05, Q06 |
| R1 and R2 mapping as a pair (opposite strands, consistent span) | layout check | Q04 |
| Share of reads that map to 16S at all | runs that are not 16S (shotgun, ITS) | Q16 (code instead of curation) |
| Region from the coordinates vs the region the paper states | a new contradiction check | new |
| Error rate per read position (3.3), compared with the reported quality | quality that was lost or binned | Q01, Q15, Q07 |

**How to build it with the standard library.** Preflight already uses the catalog primer sites as
conserved anchors (`orientation_votes`), and `pipeline/assets/primers_16s.tsv` already holds their
E. coli coordinates. Extend this in two steps:

1. Anchor each read with k-mers from a small panel of full-length references (a few dozen
   sequences across the main gut phyla, with their SILVA alignment positions mapped to E. coli).
   Shared k-mers vote for an offset and a strand, which gives the start and end coordinates
   within a few bases.
2. Run a banded alignment (a band of about ±5 nt around that offset) to the best panel
   reference. This gives mismatches by read position.

At 2000 reads per run that is seconds per run in plain Python. It stays in `pipeline/bin`, so it
runs everywhere preflight runs.

### 3.3 Statistics instead of cliffs

**Measured error rate.** A sequencing error is a mismatch against the true sequence. Take reads
whose best reference hit is near-exact over their first 100 bases. Their mismatch rate by read
position gives the run's own error profile: a run's error rate tends to rise along the read, while
real differences from the reference do not follow read position. This is measured, not taken from
the reported quality. It gives three things:
- **truncation:** cut where the measured error rate passes a target, per batch;
- **quality calibration:** whether the reported Q values mean what they say (flat Q from SRA Lite,
  Q01, shows as a calibration failure, and the measured profile replaces it);
- **the likelihood** used for classification.

Caveat: a run whose taxa have no close reference shows inflated "errors". Use only reads with a
near-exact hit, and report how many there were.

**Abundance by likelihood (EM), not by an identity threshold.**
- Each unique sequence is aligned to the candidate references within a margin of its best hit.
  Its likelihood under each candidate comes from the run's error profile.
- Expectation-maximization then estimates the abundances. A read that fits several references
  equally is shared among them in proportion to their abundance instead of being forced to one.
  This is the approach of Emu (Curry et al. 2022, Nature Methods), built for full-length nanopore
  16S. For short reads the same algorithm needs one addition: the resolvable groups below.
- Low-quality reads add uncertainty instead of wrong calls, and nothing is dropped at an identity
  cliff.

**Resolvable groups.** SILVA's alignment places every reference on the same columns. Cut every
reference down to the window a study sequenced (from the read map). References whose windows are
identical, or nearly so, cannot be told apart in that study. Group them, and report abundances per
group.
- For the cross-study model, merge two genera whenever any included study's window cannot separate
  them. The merges are listed with their reason (for example, Escherichia and Shigella are
  identical over most of the gene).
- The feature set then means the same thing in every study. Today a genus can be "unclassified" in
  one region and resolved in another.

**Primer-site mismatches: unseen is not absent.** The same alignment shows how each genus's primer
sites differ from the study's primers. A genus with several mismatches near the 3' end of a primer
is probably under-amplified in that study. A well-known example is 27F and Bifidobacterium. Mark
that feature as "not observable in this study" (missing) instead of a zero, and flag it for the
model. Check the rule on a mock community before trusting it.

### 3.4 QC as statistics: outliers within a batch, and loss by label

- **Batches.** Group runs by read length per mate, instrument, run date and error profile.
  Q05 already finds the shortest batch; generalize it.
- **Outlier within a batch, not a fixed 20 %.** A run that keeps 15 % while its batch keeps 65 %
  is a sample problem (a robust z-score on the median absolute deviation flags it). A whole batch
  at 38 % is a parameter problem, which is the question for 35756051.
- **Loss by label.** After every step that drops reads (filtering, merging, chimera removal,
  classification), test whether retention differs between cases and controls (Mann-Whitney test,
  plus the difference in medians). Flag it even when the absolute retention looks fine. E23
  would have been caught by name, and this needs no new data: `run_qc.tsv` and the sample sheets
  already hold the numbers.

### 3.5 Across studies: contrasts within a study, combined by meta-analysis

A useful model comes from McLaren, Willis & Callahan 2019 (eLife). Each protocol (primers, region,
DNA extraction, platform) multiplies every taxon's true share by its own fixed efficiency. In
log-ratios that is a **constant offset per study and taxon**. Four consequences:

- **A case-control difference inside one study cancels the offset**, because cases and controls
  share the protocol. So estimate the disease effect per genus group within each study (difference
  in mean CLR, with its standard error), then combine the studies with a random-effects
  meta-analysis.
  - The heterogeneity statistic (I²) says which genera behave consistently across studies.
  - A meta-regression on region, sample type or age group asks why others do not.
  - Duvallet et al. 2017 (Nature Communications) did this kind of within-study analysis across
    MicrobiomeHD, which also holds 23013615's labels. MMUPHin (Ma et al. 2022, Genome Biology)
    packages batch correction and meta-analysis for microbiome profiles, and its paper is an IBD
    meta-analysis; compare with it before writing our own.
- **Ranking inside a held-out study ignores the offset; a fixed threshold does not.** LOSO with
  AUROC per held-out study is the right score. The SMDI's best cutoff moved from 0 to 0.5 on its
  test set for this reason.
- **Statistics cannot remove an offset that lines up with the label** (42349924, Q21). The design
  check is the only defense. The agent should flag such studies, and the owner decides about
  training (`needs_you`).
- **Normalizing a held-out study must not use its labels.** Centering on its controls (or
  percentile normalization against controls, Gibbons et al. 2018) leaks the answer in LOSO.
  Centering on all its samples is label-free, but it depends on the case mix. Test both, and only
  label-free versions count as LOSO scores. Case-only studies (37696680) cannot calibrate their
  own offset; a region-matched healthy reference can.

The interpretable output for the dashboard is an SMDI-style **IBD dysbiosis index** per sample:
the mean CLR of the genus groups that the meta-analysis finds consistently raised in IBD, minus
the mean CLR of those consistently lowered. The genus groups are chosen inside each LOSO training
fold.

## 4. How it fits the agentic workflow

```
                         paper, curation notes ─┐
prepare ─► read map ─► parameter agent ⇄ tools (probes, batches, catalog) ─► validator ─► fetch
           (3.2)            │                                                  │ reject x2
                            └──────────────── human task ◄─────────────────────┘
fetch ─► track A (ampliseq, agent's params) ─┐
      └► track B (read map + EM, 3.3) ────────┴► statistical QC (3.4) ─► QC agent ─► load ─► meta-analysis + LOSO (3.5)
                                                                           │
                                                      catalog proposal ◄───┘  (new kind of problem)
```

**The agent's tools.** Each tool is deterministic, tested, and runs offline on fixtures.

| Tool | Returns | Built from |
|---|---|---|
| `read_map(runs)` | per run: strand, start/end coordinates, leading bases, lengths per mate, share mapping to 16S, error profile, quality calibration | preflight, 3.2 |
| `batches()` | groups of runs and what separates them | Q05 `shortest_batch` |
| `paper_claims()` | `protocol.json` fields and curation notes, each with its quote | curation |
| `probe_filter(params)` | retention per run and per batch, merge feasibility, loss by label | level 2 of section 2 |
| `probe_classify(params)` | share assigned per rank, resolvable groups for this window, unobservable genera | 3.3 |
| `catalog_lookup(symptom)` | matching Q rows and how each is handled | `failure-taxonomy.md` |
| `submit(params, evidence)` | the validator's verdict and reasons | validator |

**What the agent decides per study:**
- primer handling;
- merge or R1 alone;
- truncation per batch, or one truncation capped by the worst batch;
- which tracks to run;
- whether something contradicts the paper.

**The validator (code, not the agent) checks:**
- every value lies within its bounds;
- every non-default value cites a probe;
- the worst batch keeps enough reads, and there is no loss by label;
- pairs still merge when merging was chosen;
- the region agrees with the paper, or the contradiction is stated.

**Replay and audit.** Every tool call and model response goes to the study's `log.jsonl` and to
`ops.activity_log`, the one log that agents and people share. `params.json` gains `chosen_by` (`agent` or `rules`), the evidence ids, the model id
and the prompt hash. A rerun uses the frozen values. If the model is unavailable, the rules decide
and say so.

**The development loop.** After a run, the QC agent reads the statistical QC and the agreement
between tracks.
- A known problem gets its known handling.
- A new kind of problem becomes a catalog proposal: what was seen, a fixture, a suggested rule or
  validator check. A person approves it, and it becomes a tool, a check and a test. This is the
  existing rule in CLAUDE.md, with the agent drafting the first version.

## 5. Evaluation: showing that the agent works

This is the part that makes it AI engineering rather than a demo.

- **Replay set.** For the eight processed studies, take the preflight inputs and the measured
  outcomes as offline cases. Check whether the agent reaches parameters at least as good as the
  ones that were finally run, and whether it flags what QC later found.
- **Fault injection from the catalog.** Each row becomes a perturbation of clean reads with a known
  right answer.

| Catalog row | Injected fault | Expected behaviour |
|---|---|---|
| Q05 | shorten R1/R2 by one base in 20 % of runs | per-batch or capped truncation; worst batch keeps its reads |
| Q08 | reverse-complement single-end reads | strand found; handled |
| Q09, Q11, Q12 | strip primers; prepend a 0–8 nt spacer; clip the primer's 5' end | handled from the start coordinates |
| Q14 | delete the primers from `protocol.json` | primers inferred from the coordinates |
| Q01, Q15 | set every quality score to one value; bin qualities to 4 values | calibration failure detected; measured error profile used, or NCBI fallback |
| Q04 | declare SINGLE while `_1`/`_2` files exist | pair detected |
| Q16 | mix runs of shotgun reads into the study | non-16S runs excluded |
| new | the paper says V3–V4, the reads are V4 | contradiction flagged |
| Q21 | make the short batch all controls | loss by label flagged |

- **What to score:**
  - detection, right handling and validator acceptance;
  - false alarms on clean cases;
  - tool calls, tokens, cost and wall time per study.

  Score the rules-only baseline and each model (for example Haiku and Sonnet) side by side. The
  result is a table for the dashboard and the job talk.
- **CI stays offline.** Fixtures are synthetic reads, or a few hundred public reads per case, and
  model responses are recorded. Live-model evaluation runs on demand. This matches the rule that
  tests use no network and no LLM.

## 6. Costs and risks

- **Compute.**
  - The read map and the probes are seconds per run on reads preflight already streams.
  - Track B aligns unique sequences against SILVA. 37661264 has about 1.08 M unique R1
    sequences, so the time is unknown until measured on one study.
  - EM over that many sequences needs numpy: a container or a `uv` group, not `pipeline/bin`.
  - Check that the tools run on osx-arm64 and in Docker first (E1, E2).
- **The error profile can be inflated** by taxa without close references (3.3). Report the share
  of reads it rests on.
- **EM cannot separate references the region cannot separate.** The resolvable groups are the
  answer to that, not a tuning knob.
- **Primer-mismatch rules** are predictions; check them against a mock community.
- **Model cost and latency** per study are unknown; the evaluation suite measures them.
- **More moving parts.** Two tracks and an agent are more to maintain than one pinned workflow.
  Track B earns its place only if the probe numbers show it (section 7, step 5).

## 7. Build order

Each step has a stop point, and the cheap, data-free steps come first.

1. **Loss-by-label and batch-outlier checks in `qc_gate.py`.** They need no new data: a dry check
   on the eight studies' `run_qc.tsv` and sample sheets shows what they flag. They would have named
   E23's runs and would say whether 35756051's 42 runs line up with a group.
2. **The read map in preflight** (3.2, standard library). Run it on the eight studies' streamed
   reads and compare with what preflight decided. It adds Q16 detection and the region check.
3. **Probe tools and the validator.** Send today's rule-based parameters through the validator
   too, so the baseline is scored in the same way.
4. **The parameter agent** with the tools of section 4, first through `claude -p` with Haiku (as
   discovery does), later in LangGraph. Run it on the replay set and the fault suite, against the
   rules-only baseline.
5. **Track B** (read map, measured errors, EM, resolvable groups) on one study, then on 35756051,
   36111238, 28174747 and 29335008. The owner deferred this on 2026-11-18. Estimate: one re-fetch
   per study (about 20 min and $0.10 on Batch); classification time measured on the first study.
6. **Meta-analysis, harmonized genus groups and the dysbiosis index** in `ml/`, beside the current
   LOSO.
7. **Region offsets measured separately from study offsets.** Today region and study are fully
   confounded (`ml-results.md`). Two sources would help: a mock community sequenced in several
   regions (for example Kozich et al. 2013, the data behind mothur's MiSeq SOP; check the
   accessions), and HMP stool samples sequenced in both V1–V3 and V3–V5.

## 8. For the job talk

Every study broke the pipeline in a new way. Each break became a rule and a test, and the rules
became the tools of an agent. The agent looks at the reads through those tools, sets per-study
thresholds by measuring, and hands its choice to code that checks it. Underneath sits one idea
from classic sequence analysis: put every read on the same map of the 16S gene, and let statistics
(error rates, likelihoods, contrasts within each study) do what fixed cutoffs did badly. A test
suite built from the failure catalog shows how well the agent does, and at what cost.
