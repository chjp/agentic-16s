"""Unit tests for ml/loso.py: features, splits without leakage, and the SQL name cleaning."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loso  # noqa: E402


def toy():
    X = pd.DataFrame({"A": [5, 0, 3, 0, 9, 1], "B": [0, 0, 0, 0, 4, 0], "C": [1, 2, 3, 4, 5, 6]},
                     index=[f"s{i}" for i in range(6)])
    studies = pd.Series(["x", "x", "y", "y", "z", "z"], index=X.index)
    return X, studies


def test_clr_rows_sum_to_zero():
    X, _ = toy()
    assert np.allclose(loso.clr(X).sum(axis=1), 0)


def test_shared_genera_counts_studies_not_samples():
    X, studies = toy()
    assert loso.shared_genera(X, studies, 3) == ["A", "C"]   # B only in study z
    assert loso.shared_genera(X, studies, 1) == ["A", "B", "C"]


def test_genus_filter_ignores_the_held_out_study():
    X, studies = toy()
    train = (studies != "z").to_numpy()
    # B appears only in the held-out study z: it must not become a feature
    assert "B" not in loso.shared_genera(X[train], studies[train], 1)


def test_loso_never_trains_on_the_test_study():
    _, studies = toy()
    seen = []
    for held, train, test in loso.loso_splits(studies):
        assert set(studies[test]) == {held}
        assert held not in set(studies[train])
        seen.append(held)
    assert seen == ["x", "y", "z"]


def test_within_study_folds_keep_subjects_together():
    rng = np.random.default_rng(0)
    groups = np.repeat([f"g{i}" for i in range(12)], 3)       # 12 subjects, 3 samples each
    y = np.repeat([1] * 6 + [0] * 6, 3)
    folds = loso.within_splits(y, groups)
    assert folds is not None
    for tr, te in folds:
        assert not set(groups[tr]) & set(groups[te])
    assert rng is not None


def test_within_study_skips_one_class():
    assert loso.within_splits(np.ones(10, int), np.arange(10).astype(str)) is None


def test_score_handles_one_class():
    out = loso.score(np.ones(4, int), np.array([0.9, 0.2, 0.7, 0.6]))
    assert np.isnan(out["auroc"]) and out["sensitivity"] == 0.75


@pytest.fixture(scope="module")
def conn():
    psycopg = pytest.importorskip("psycopg")
    try:
        c = psycopg.connect(loso.database_url(), connect_timeout=3)
    except Exception:  # noqa: BLE001
        pytest.skip("database not reachable")
    yield c
    c.close()


@pytest.mark.parametrize("raw,clean", [("Clostridium--other", "Clostridium"), ("Rubrivivax--other+Ideonella",
                                        "Rubrivivax+Ideonella"), ("", None), (None, None), (" Blautia ", "Blautia")])
def test_clean_taxon(conn, raw, clean):
    assert conn.execute("select analysis.clean_taxon(%s)", (raw,)).fetchone()[0] == clean
