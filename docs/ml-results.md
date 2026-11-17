# Can the database separate IBD from healthy? (Phase 6.1, 2026-11-16)

**Yes, across studies it has never seen.** A genus-level model trained on four studies and tested on the
fifth reaches a mean AUROC of 0.71–0.74 (all sample types) and 0.78–0.80 (stool only). Every held-out
study scores well above a label-shuffled baseline, except one small biopsy study.

Everything below comes from PostgreSQL: samples and labels from `analysis.ml_samples`, features from
`analysis.genus_counts` (`db/migrations/009_genus_counts.sql`), code in `ml/loso.py`, full outputs in
`ml/results/2026-11-16_*` (metrics, per-sample predictions, coefficients, parameters with the code hash).

## Data

Five IBD 16S studies, processed by one pinned workflow (nf-core/ampliseq 3.1.0, DADA2, SILVA 144):

| Study | Region, platform | Sample types | IBD / healthy in the model |
|---|---|---|---|
| 23013615 | V3-V5, 454 | stool + biopsy | 185 / 24 |
| 28174747 | V4, HiSeq 1×101 | stool | 36 / 54 (controls are family members) |
| 29335008 | V6-V8, MiSeq | biopsy | 18 / 20 |
| 35756051 | V3-V4, MiSeq | stool | 41 / 22 |
| 37661264 | V3-V4, MiSeq | stool + biopsy | 83 / 14 |

Samples with fewer than 1,000 non-chimeric reads, or whose run failed, are left out (497 of the cohort's 544 samples remain).

**Why genus level:** the studies sequence different parts of the 16S gene, so not a single ASV is shared
between any two studies. At genus level, 130 genera are present in all five.

## Method

- **Features:** read counts per genus. Off-target reads are removed in the database view (mitochondria,
  chloroplasts, no domain), and reads without a genus go to the lowest known rank plus `_unclassified`.
  Inside each training fold, only genera present in ≥ 3 training studies are kept; the held-out study
  never shapes the feature set. Counts get a pseudocount of 0.5 and a centred log-ratio (CLR) transform.
- **Models:** L2 logistic regression (standardized, balanced class weights) and a random forest
  (500 trees, balanced).
- **Main score, leave-one-study-out (LOSO):** train on four studies, test on the fifth, five times.
  A random split would let the model score by recognizing which study a sample came from.
- **Reference, within-study CV:** stratified, grouped by subject or family (`cv_group`), so one person's
  samples never sit on both sides.
- **Baseline:** 100 runs of LOSO with labels shuffled inside each training study.

## Results: all sample types (`ibd_vs_healthy_broad`)

| Held-out study | n (IBD/healthy) | Logistic regression AUROC | Random forest AUROC | Shuffled-label AUROC (p) | Within-study AUROC (logreg) |
|---|---|---|---|---|---|
| 23013615 | 209 (185/24) | 0.74 | 0.74 | 0.48 (p = 0.010) | 0.84 |
| 28174747 | 90 (36/54) | 0.67 | 0.64 | 0.49 (p = 0.040) | 0.79 |
| 29335008 | 38 (18/20) | 0.56 | 0.65 | 0.46 (p = 0.218) | 0.78 |
| 35756051 | 63 (41/22) | 0.74 | 0.71 | 0.52 (p = 0.040) | 0.87 |
| 37661264 | 97 (83/14) | 0.85 | 0.95 | 0.53 (p = 0.010) | 0.97 |
| **Mean** | | **0.71** | **0.74** | | 0.85 |

## Results: stool only

| Held-out study | n (IBD/healthy) | Logistic regression AUROC | Random forest AUROC | Shuffled-label AUROC (p) |
|---|---|---|---|---|
| 23013615 | 134 (116/18) | 0.79 | 0.77 | 0.49 (p = 0.020) |
| 28174747 | 90 (36/54) | 0.73 | 0.74 | 0.49 (p = 0.020) |
| 35756051 | 63 (41/22) | 0.77 | 0.74 | 0.51 (p = 0.010) |
| 37661264 | 52 (38/14) | 0.84 | 0.95 | 0.51 (p = 0.020) |
| **Mean** | | **0.78** | **0.80** | |

## What the model relies on

Genera the logistic regression weighs most (all samples; positive = more in IBD):

- **Lower in IBD:** *Roseburia*, *Agathobacter*, *Segatella*, *Romboutsia*, *Marvinbryantia*. *Roseburia*
  and *Agathobacter* are butyrate producers, whose loss in IBD is one of the most consistent findings in
  the literature. Mean relative abundance of *Agathobacter*: 1.4 % in IBD vs 3.4 % in healthy samples.
- **Higher in IBD:** *Enterococcus*, *Haemophilus*, *Eggerthella*, *Dialister*, *Faecalimonas*,
  *Eisenbergiella*. *Enterococcus* (0.96 % vs 0.01 %), *Haemophilus* and *Eggerthella* are among the
  taxa repeatedly reported as enriched in IBD.

So the cross-study signal is largely known IBD biology, not batch artefacts.

## What the numbers say

1. **The goal is met.** On studies it never trained on, with a different 16S region and platform each
   time, the model ranks an IBD sample above a healthy one about 3 times out of 4. Four of five held-out
   studies are clearly above chance.
2. **Crossing studies costs about 0.14 AUROC** (within-study mean 0.85 vs LOSO 0.71). That gap is what
   different regions, platforms, labs and cohorts do to a model; it is the batch effect the project plans
   to study (Phase 7.2).
3. **Mixing tissue types hurts.** Stool only raises the mean from 0.71 to 0.78. The worst study,
   29335008, is the only all-biopsy study with a region no other study uses (V6-V8); with 38 samples it is
   not distinguishable from chance when held out (p = 0.22), although its within-study AUROC is 0.78.
4. **The random forest is a little better across studies** (0.74 / 0.80), the logistic regression is
   easier to read. Both are reported; neither was tuned.

## Limits

- Region and study are fully confounded (one region per study, except V3-V4 twice), so these results
  cannot separate a region effect from a lab or cohort effect.
- Controls differ: family members (28174747), healthy volunteers, and non-IBD patients in some studies.
- Classes are unbalanced in 23013615 and 37661264 (few healthy samples); AUROC is used for that reason.
- 23013615 has several samples from some subjects, and 37661264 has 45 biopsies without a subject
  identifier. LOSO is unaffected (whole studies are held out), but within-study CV treats those 45 as one group.
- Three studies were denoised with DADA2 pooled inference and two with pseudo-pooling (owner's decision for
  speed); the per-study denoising mode is recorded in `bio.pipeline_run` and `params.json`.
- 35756051 has an open QC review (quality filtering kept under 20 % of reads in 42 runs); its low-depth
  samples are excluded by the 1,000-read cutoff.

## Reproduce

```bash
uv run --with 'psycopg[binary]' python db/migrate.py
uv run --with scikit-learn --with pandas --with 'psycopg[binary]' python ml/loso.py
uv run --with scikit-learn --with pandas --with 'psycopg[binary]' python ml/loso.py --sample-type stool
uv run --with pytest --with scikit-learn --with pandas --with 'psycopg[binary]' pytest ml/tests
```
