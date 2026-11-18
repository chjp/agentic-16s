import json
from pathlib import Path

import sra_source

FIX = Path(__file__).parent / "fixtures" / "ncbi_sdl_SRR578443.json"


def test_picks_normalized_not_lite_or_sff():
    loc = sra_source.pick_normalized(json.loads(FIX.read_text()))
    assert loc["name"] == "SRR578443"            # not SRR578443.lite (noqual) or the SFF (rehydration)
    assert loc["url"].startswith("https://sra-pub-run-odp.s3.amazonaws.com/")
    assert loc["md5"] == "56190dd1d468550f6f19436981e5bc88"


def test_none_when_only_lite():
    sdl = json.loads(FIX.read_text())
    for res in sdl["result"]:
        res["files"] = [f for f in res["files"] if f.get("noqual")]
    assert sra_source.pick_normalized(sdl) is None


def test_skips_paid_locations():
    sdl = {"result": [{"files": [{"type": "sra", "name": "X", "md5": "m",
                                  "locations": [{"service": "gs", "payRequired": True, "link": "https://g/x"}]}]}]}
    assert sra_source.pick_normalized(sdl) is None


def test_mate_files_ignore_the_unpaired_reads_file(tmp_path):
    """--split-3 writes unpaired reads to <run>.fastq.gz, which sorts before <run>_1 (E22)."""
    for name in ("SRR1.fastq.gz", "SRR1_1.fastq.gz", "SRR1_2.fastq.gz", "SRR10_1.fastq.gz"):
        (tmp_path / name).write_bytes(b"")
    assert [p.name for p in sra_source.mate_files(tmp_path, "SRR1")] == ["SRR1_1.fastq.gz", "SRR1_2.fastq.gz"]
    assert sra_source.mate_files(tmp_path, "SRR2") == []


def test_fetch_split_reuses_files_preflight_already_dumped(tmp_path, monkeypatch):
    import gzip
    for m in (1, 2):
        with gzip.open(tmp_path / f"SRR1_{m}.fastq.gz", "wt") as fh:
            fh.write("@r\nACGT\n+\nIIII\n" * 3)
    monkeypatch.setattr(sra_source, "dump_cmd", lambda *a: ["fastq-dump"])
    monkeypatch.setattr(sra_source, "locate", lambda run, cache=None: {"md5": "x", "size": 10, "url": "u"})
    monkeypatch.setattr(sra_source, "download_sra", lambda *a: (_ for _ in ()).throw(AssertionError("no download")))
    f = sra_source.fetch_split("SRR1", tmp_path)
    assert f["reused"] and f["reads"] == 3 and [p.rsplit("/", 1)[-1] for p in f["paths"]] == ["SRR1_1.fastq.gz", "SRR1_2.fastq.gz"]
