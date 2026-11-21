#!/usr/bin/env python3
"""Phase 6.1: can the database separate IBD from healthy? Genus-level classifier, judged study by study.

Reads samples and labels from analysis.ml_samples and genus counts from analysis.genus_counts
(PostgreSQL), then reports:
  * leave-one-study-out (LOSO): train on all studies but one, test on the held-out one. This is the
    main score (README.md): a random split would reward recognizing which study a sample came from.
  * within-study cross-validation, grouped by subject/family (cv_group), as a reference. The gap
    between the two is what crossing studies (region, platform, lab) costs.
  * a label-permutation baseline for the LOSO logistic regression.
  * logistic-regression coefficients per genus, as a biology check.

Features: per sample, counts of the genera seen in at least --min-studies of the TRAINING studies
(chosen inside each fold, so the held-out study never shapes the feature set), turned into
centred log-ratios (CLR) with a pseudocount of 0.5 reads.

  uv run --with scikit-learn --with pandas --with 'psycopg[binary]' python ml/loso.py
  uv run ... python ml/loso.py --sample-type stool --name stool_only
Writes ml/results/<date>_<name>/ (params, metrics, predictions, coefficients, summary.md).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score, roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
PSEUDOCOUNT = 0.5
SEED = 100


# ---------------------------------------------------------------- data

def database_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.strip().startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    sys.exit("DATABASE_URL is not set (environment or .env)")


def load(cohort: str, sample_type: str | None, min_reads: int) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Samples (one row each) and a sample x genus count matrix. Samples without genus counts
    (failed or skipped runs) drop out; so do samples under min_reads non-chimeric reads."""
    import psycopg
    with psycopg.connect(database_url()) as conn:
        conn.execute("refresh materialized view analysis.genus_counts")
        samples = pd.read_sql(
            """select sample_id, study_key, label, cv_group, diagnosis, control_type, sample_type,
                      variable_region, platform, reads_nonchim, cohort_version
               from analysis.ml_samples where cohort = %(c)s""", conn, params={"c": cohort})
        counts = pd.read_sql(
            "select sample_id, genus, count from analysis.genus_counts where sample_id = any(%(ids)s)",
            conn, params={"ids": samples.sample_id.tolist()})
        runs = pd.read_sql(
            """select st.study_key, pr.pipeline_run_id, pr.params->>'sample_inference' as sample_inference,
                      pr.params->>'_software_environment' as environment
               from bio.pipeline_run pr join core.study st using (study_id) where pr.is_current
               order by st.study_key""", conn)
    if samples.empty:
        sys.exit(f"cohort {cohort!r} has no samples")
    n_cohort = len(samples)
    if sample_type:
        samples = samples[samples.sample_type == sample_type]
    samples = samples[samples.sample_id.isin(counts.sample_id) & (samples.reads_nonchim >= min_reads)]
    samples = samples.drop_duplicates("sample_id").set_index("sample_id").sort_index()
    X = (counts[counts.sample_id.isin(samples.index)]
         .pivot_table(index="sample_id", columns="genus", values="count", aggfunc="sum", fill_value=0)
         .reindex(samples.index, fill_value=0))
    info = {"cohort_samples": n_cohort, "pipeline_runs": runs.to_dict("records")}
    return samples, X, info


# ---------------------------------------------------------------- features

def shared_genera(X: pd.DataFrame, studies: pd.Series, min_studies: int) -> list[str]:
    """Genera with at least one read in at least min_studies of the given samples' studies."""
    present = (X > 0).groupby(studies.values).any()
    return present.columns[present.sum() >= min_studies].tolist()


def clr(counts: pd.DataFrame, pseudocount: float = PSEUDOCOUNT) -> pd.DataFrame:
    """Centred log-ratio of counts + pseudocount; each row sums to 0."""
    logx = np.log(counts.to_numpy(dtype=float) + pseudocount)
    return pd.DataFrame(logx - logx.mean(axis=1, keepdims=True), index=counts.index, columns=counts.columns)


def models() -> dict:
    return {
        "logreg": make_pipeline(StandardScaler(), LogisticRegression(
            C=0.1, class_weight="balanced", max_iter=5000)),
        "forest": RandomForestClassifier(n_estimators=500, min_samples_leaf=2, class_weight="balanced_subsample",
                                         random_state=SEED, n_jobs=-1),
    }


# ---------------------------------------------------------------- splits

def loso_splits(studies: pd.Series):
    """(held-out study, train mask, test mask) for every study."""
    for s in sorted(studies.unique()):
        test = (studies == s).to_numpy()
        yield s, ~test, test


def within_splits(y: np.ndarray, groups: np.ndarray, max_splits: int = 5):
    """Grouped, stratified folds inside one study; None when a class has too few groups to split."""
    n = min(max_splits, *(len(set(groups[y == c])) for c in (0, 1)))
    if n < 2:
        return None
    return list(StratifiedGroupKFold(n_splits=n, shuffle=True, random_state=SEED).split(np.zeros(len(y)), y, groups))


# ---------------------------------------------------------------- scoring

def score(y: np.ndarray, p: np.ndarray) -> dict:
    out = {"n": len(y), "n_ibd": int(y.sum()), "n_healthy": int((1 - y).sum())}
    out["auroc"] = roc_auc_score(y, p) if 0 < y.sum() < len(y) else np.nan
    pred = (p >= 0.5).astype(int)
    out["sensitivity"] = (pred[y == 1] == 1).mean() if y.sum() else np.nan
    out["specificity"] = (pred[y == 0] == 0).mean() if (1 - y).sum() else np.nan
    out["balanced_accuracy"] = balanced_accuracy_score(y, pred) if 0 < y.sum() < len(y) else np.nan
    return out


def fit_predict(model, X: pd.DataFrame, y: np.ndarray, studies: pd.Series, train: np.ndarray, test: np.ndarray,
                min_studies: int) -> np.ndarray:
    genera = shared_genera(X[train], studies[train], min(min_studies, studies[train].nunique()))
    model.fit(clr(X.loc[train, genera]), y[train])
    return model.predict_proba(clr(X.loc[test, genera]))[:, 1]


def run_loso(X, samples, min_studies):
    y, studies = samples.label.to_numpy(), samples.study_key
    rows, preds = [], []
    for name, model in models().items():
        for held, train, test in loso_splits(studies):
            p = fit_predict(model, X, y, studies, train, test, min_studies)
            rows.append({"model": name, "held_out": held, **score(y[test], p)})
            preds.append(pd.DataFrame({"model": name, "eval": "loso", "sample_id": X.index[test],
                                       "study_key": held, "label": y[test], "p_ibd": p}))
    return pd.DataFrame(rows), pd.concat(preds)


def run_within(X, samples, min_studies):
    rows, preds = [], []
    for study, sub in samples.groupby("study_key"):
        y, groups = sub.label.to_numpy(), sub.cv_group.to_numpy()
        folds = within_splits(y, groups)
        if folds is None:
            rows += [{"model": m, "study": study, "n": len(y), "n_ibd": int(y.sum()),
                      "n_healthy": int((1 - y).sum()), "note": "one class or too few subjects"} for m in models()]
            continue
        Xs = X.loc[sub.index]
        one_study = pd.Series("x", index=sub.index)  # inside one study every genus filter is "this study"
        for name, model in models().items():
            p = np.full(len(y), np.nan)
            for tr, te in folds:
                tr_mask, te_mask = np.zeros(len(y), bool), np.zeros(len(y), bool)
                tr_mask[tr], te_mask[te] = True, True
                p[te] = fit_predict(model, Xs, y, one_study, tr_mask, te_mask, 1)
            rows.append({"model": name, "study": study, "folds": len(folds), **score(y, p)})
            preds.append(pd.DataFrame({"model": name, "eval": "within_study", "sample_id": sub.index,
                                       "study_key": study, "label": y, "p_ibd": p}))
    return pd.DataFrame(rows), (pd.concat(preds) if preds else pd.DataFrame())


def run_permutations(X, samples, min_studies, n_perm, observed: pd.DataFrame) -> pd.DataFrame:
    """LOSO logistic regression with labels shuffled inside each training study (keeps each study's
    case/control balance), scored against the true labels of the held-out study."""
    rng = np.random.default_rng(SEED)
    y, studies = samples.label.to_numpy(), samples.study_key
    null = {s: [] for s in studies.unique()}
    for _ in range(n_perm):
        y_perm = y.copy()
        for s in studies.unique():
            idx = np.flatnonzero((studies == s).to_numpy())
            y_perm[idx] = rng.permutation(y[idx])
        for held, train, test in loso_splits(studies):
            if not 0 < y[test].sum() < test.sum():
                continue
            p = fit_predict(models()["logreg"], X, y_perm, studies, train, test, min_studies)
            null[held].append(roc_auc_score(y[test], p))
    obs = observed[observed.model == "logreg"].set_index("held_out").auroc
    rows = []
    for s, vals in null.items():
        if not vals:
            continue
        vals = np.array(vals)
        rows.append({"held_out": s, "observed_auroc": obs[s], "null_mean": vals.mean(),
                     "null_p95": np.quantile(vals, 0.95), "p_value": (1 + (vals >= obs[s]).sum()) / (1 + len(vals))})
    return pd.DataFrame(rows)


def coefficients(X, samples, min_studies) -> pd.DataFrame:
    """Logistic regression on all samples; positive = higher (CLR) in IBD."""
    y, studies = samples.label.to_numpy(), samples.study_key
    genera = shared_genera(X, studies, min_studies)
    model = models()["logreg"].fit(clr(X[genera]), y)
    rel = X[genera].div(X.sum(axis=1), axis=0)
    return (pd.DataFrame({"genus": genera, "coef": model[-1].coef_[0],
                          "mean_rel_ibd": rel[y == 1].mean().to_numpy(), "mean_rel_healthy": rel[y == 0].mean().to_numpy(),
                          "n_studies": (X[genera] > 0).groupby(studies.values).any().sum().to_numpy()})
            .sort_values("coef"))


# ---------------------------------------------------------------- report

def summary_md(params, loso, within, perm, coef) -> str:
    def fmt(v):
        return "n/a" if pd.isna(v) else f"{v:.2f}"
    lines = [f"# {params['name']}: IBD vs healthy, genus level", "",
             f"Cohort `{params['cohort']}`" + (f", sample type {params['sample_type']}" if params["sample_type"] else "")
             + f", min reads {params['min_reads']}, genera in >= {params['min_studies']} training studies, CLR.", "",
             "## Leave-one-study-out (main score)", "",
             "| Held-out study | n (IBD/healthy) | logreg AUROC | forest AUROC | logreg bal. acc. | null AUROC mean (p) |",
             "|---|---|---|---|---|---|"]
    for s in sorted(loso.held_out.unique()):
        a = loso[(loso.held_out == s) & (loso.model == "logreg")].iloc[0]
        b = loso[(loso.held_out == s) & (loso.model == "forest")].iloc[0]
        pr = perm[perm.held_out == s]
        null = f"{pr.null_mean.iloc[0]:.2f} (p={pr.p_value.iloc[0]:.3f})" if len(pr) else "n/a"
        lines.append(f"| {s} | {a.n} ({a.n_ibd}/{a.n_healthy}) | {fmt(a.auroc)} | {fmt(b.auroc)} | {fmt(a.balanced_accuracy)} | {null} |")
    for m in ("logreg", "forest"):
        lines.append(f"\nMean LOSO AUROC, {m}: {fmt(loso[loso.model == m].auroc.mean())}")
    lines += ["", "## Within-study grouped CV (reference)", "", "| Study | model | AUROC | note |", "|---|---|---|---|"]
    for _, r in within.iterrows():
        lines.append(f"| {r.study} | {r.model} | {fmt(r.get('auroc', np.nan))} | {r.get('note', '') if isinstance(r.get('note', ''), str) else ''} |")
    lines += ["", "## Genera the logistic regression leans on (all samples)", "",
              "| Genus | coef | mean rel. IBD | mean rel. healthy | studies |", "|---|---|---|---|---|"]
    top = pd.concat([coef.head(10), coef.tail(10)[::-1]])
    for _, r in top.iterrows():
        lines.append(f"| {r.genus} | {r.coef:+.2f} | {r.mean_rel_ibd:.4f} | {r.mean_rel_healthy:.4f} | {r.n_studies} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cohort", default="ibd_vs_healthy_broad")
    ap.add_argument("--sample-type", default=None, help="e.g. stool; default all types in the cohort")
    ap.add_argument("--min-reads", type=int, default=1000)
    ap.add_argument("--min-studies", type=int, default=3)
    ap.add_argument("--permutations", type=int, default=100)
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    name = a.name or (f"{a.cohort}" + (f"_{a.sample_type}" if a.sample_type else ""))
    out = ROOT / "ml" / "results" / f"{dt.date.today():%Y-%m-%d}_{name}"
    out.mkdir(parents=True, exist_ok=True)

    samples, X, info = load(a.cohort, a.sample_type, a.min_reads)
    print(f"{len(samples)} samples, {X.shape[1]} genera, studies: "
          + ", ".join(f"{s} {int(g.label.sum())}/{int((1 - g.label).sum())}" for s, g in samples.groupby("study_key")))
    loso, loso_pred = run_loso(X, samples, a.min_studies)
    within, within_pred = run_within(X, samples, a.min_studies)
    perm = run_permutations(X, samples, a.min_studies, a.permutations, loso)
    coef = coefficients(X, samples, a.min_studies)

    import sklearn
    params = {"name": name, "cohort": a.cohort, "cohort_version": int(samples.cohort_version.iloc[0]),
              "sample_type": a.sample_type, "min_reads": a.min_reads, "min_studies": a.min_studies,
              "permutations": a.permutations, "pseudocount": PSEUDOCOUNT, "seed": SEED,
              "n_samples": len(samples), "n_genera_total": X.shape[1], **info,
              "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "sklearn": sklearn.__version__, "run_at": dt.datetime.now().isoformat(timespec="seconds")}
    (out / "params.json").write_text(json.dumps(params, indent=1, default=str))
    loso.to_csv(out / "metrics_loso.csv", index=False)
    within.to_csv(out / "metrics_within_study.csv", index=False)
    perm.to_csv(out / "permutation_loso_logreg.csv", index=False)
    coef.to_csv(out / "coefficients_logreg.csv", index=False)
    pd.concat([loso_pred, within_pred]).to_csv(out / "predictions.csv", index=False)
    samples.reset_index().to_csv(out / "samples.csv", index=False)
    text = summary_md(params, loso, within, perm, coef)
    (out / "summary.md").write_text(text)
    print(text)
    print(f"written to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
