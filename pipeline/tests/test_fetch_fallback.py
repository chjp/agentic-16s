"""ENA -> NCBI fallback for runs whose ENA FASTQ cannot be downloaded (35756051, SRR15400852, 2026-11-15)."""
import gzip
from pathlib import Path

import pytest

import run_study
import sra_source

FIX = Path(__file__).parent / "fixtures" / "fetchngs_md5_failure.txt"


def test_failed_runs_only_from_final_error():
    # SRR15402225 failed once and succeeded on retry; only SRR15400852 failed for good
    assert run_study.failed_ena_runs(FIX.read_text()) == ["SRR15400852"]


def test_failed_runs_none_for_other_errors():
    assert run_study.failed_ena_runs("Caused by:\n  Process `NFCORE_FETCHNGS:FETCHNGS:SRA_RUNINFO (x)` terminated") == []


def test_fallback_limit():
    assert run_study.fallback_limit(6) == 3
    assert run_study.fallback_limit(89) == 4
    assert run_study.fallback_limit(400) == 20


def test_split_dump_uses_ena_read_names():
    args = sra_source.SPLIT_DUMP
    assert "--split-3" in args and "--gzip" in args
    assert args[args.index("--defline-seq") + 1] == "@$ac.$si $si/$ri"


@pytest.mark.skipif(not sra_source.dump_cmd(sra_source.SPLIT_DUMP), reason="sra-tools not installed")
def test_fetch_split_real_run(tmp_path):
    """Network test: SRR15400852 from NCBI, both mates, ENA-style headers (about 17 MB)."""
    res = sra_source.fetch_split("SRR15400852", tmp_path)
    names = sorted(Path(p).name for p in res["paths"])
    assert names == ["SRR15400852_1.fastq.gz", "SRR15400852_2.fastq.gz"]
    assert res["reads"] == 43838
    with gzip.open(res["paths"][0], "rt") as fh:
        assert fh.readline().strip() == "@SRR15400852.1 1/1"
    assert not list(tmp_path.glob("*.sra")) and not list(tmp_path.glob(".*ncbi"))
