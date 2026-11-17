"""collect -> qc -> cleanup on a tiny synthetic ampliseq output directory."""
import json

import pytest

import collect
import common
import qc_gate
import run_study

SEQS = {"a": "TACGGAGGATCCGAGCGTTATCCGGATTTATTGGGTTTAAAGGGAGCG" * 9,
        "b": "TACGTAGGTGGCAAGCGTTGTCCGGATTTATTGGGCGTAAAGCGAGCG" * 9,
        "c": "TACGGAGGGTGCAAGCGTTAATCGGAATTACTGGGCGTAAAGCGCACG" * 9}


@pytest.fixture
def study(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(common, "STUDIES", tmp_path / "studies")
    pmid, tag = "999", "main"
    (tmp_path / "studies" / pmid).mkdir(parents=True)
    common.write_tsv(tmp_path / "studies" / pmid / "sample_sheet.tsv",
                     [{"run_accession": "SRR1", "group": "case"}, {"run_accession": "SRR2", "group": "control"}])
    d = common.protocol_dir(pmid, tag)
    ids = {k: common.md5(s) for k, s in SEQS.items()}
    common.write_json(d / "params.json", {"params": {"dada_ref_taxonomy": "silva=144", "sequencing_type": "illumina_pe"},
                                          "reasons": {}, "observed": {"orientation": "forward",
                                                                      "expected_amplicon_length": 432}})
    common.write_tsv(d / "samplesheet.tsv", [{"sample": "SRR1", "fastq_1": "x", "fastq_2": "y", "run": "1"},
                                             {"sample": "SRR2", "fastq_1": "x", "fastq_2": "y", "run": "1"}])
    dada2 = d / "ampliseq" / "dada2"
    dada2.mkdir(parents=True)
    (dada2 / "ASV_seqs.fasta").write_text("".join(f">{ids[k]}\n{s}\n" for k, s in SEQS.items()))
    common.write_tsv(dada2 / "ASV_table.tsv", [{"ASV_ID": ids["a"], "SRR1": 1500, "SRR2": 0},
                                               {"ASV_ID": ids["b"], "SRR1": 500, "SRR2": 2400},
                                               {"ASV_ID": ids["c"], "SRR1": 0, "SRR2": 100}])
    common.write_tsv(dada2 / "DADA2_stats.tsv", [
        {"sample": "SRR1", "DADA2_input": 2500, "filtered": 2300, "denoisedF": 2200, "denoisedR": 2150,
         "denoisedPairs": 2100, "merged": 2050, "nonchim": 2000},
        {"sample": "SRR2", "DADA2_input": 3000, "filtered": 2800, "denoisedF": 2700, "denoisedR": 2700,
         "denoisedPairs": 2650, "merged": 2600, "nonchim": 2500}])
    common.write_tsv(dada2 / "ASV_tax_species.silva.tsv", [
        {"ASV_ID": ids["a"], "Domain": "Bacteria", "Phylum": "Bacteroidota", "Class": "Bacteroidia",
         "Order": "Bacteroidales", "Family": "Bacteroidaceae", "Genus": "Bacteroides", "Species": "",
         "confidence": 1.0, "genus_confidence": 0.98, "sequence": SEQS["a"]},
        {"ASV_ID": ids["b"], "Domain": "Bacteria", "Phylum": "Bacillota", "Class": "Clostridia",
         "Order": "Oscillospirales", "Family": "Ruminococcaceae", "Genus": "Faecalibacterium", "Species": "",
         "confidence": 0.9, "genus_confidence": 0.9, "sequence": SEQS["b"]},
        {"ASV_ID": ids["c"], "Domain": "Bacteria", "Phylum": "", "Class": "", "Order": "", "Family": "",
         "Genus": "", "Species": "", "confidence": 0.6, "genus_confidence": "NA", "sequence": SEQS["c"]}])
    fq = common.data_dir() / "fastq" / pmid / tag / "fastq"
    fq.mkdir(parents=True)
    for r in ("SRR1", "SRR2"):
        for m in (1, 2):
            (fq / f"{r}_{m}.fastq.gz").write_bytes(b"x" * 100)
    return pmid, tag, d, fq


def test_collect_writes_schema_tables(study):
    pmid, tag, d, _ = study
    integrity = collect.collect(pmid, tag, {"exit_code": 0})
    assert integrity["pass"], integrity["problems"]
    t = d / "tables"
    ab = common.read_tsv(t / "abundance.tsv")
    assert len(ab) == 4 and all(int(r["count"]) > 0 for r in ab)
    asv = common.read_tsv(t / "asv.tsv")
    assert all(common.md5(a["sequence"]) == a["sequence_md5"] for a in asv)
    tax = {r["sequence_md5"]: r for r in common.read_tsv(t / "taxonomy_assignment.tsv")}
    assert tax[common.md5(SEQS["b"])]["genus"] == "Faecalibacterium"
    assert json.loads(tax[common.md5(SEQS["a"])]["confidence"]) == {"genus": 0.98}
    qc = {r["run_accession"]: r for r in common.read_tsv(t / "run_qc.tsv")}
    assert qc["SRR1"]["status"] == "ok" and qc["SRR2"]["reads_nonchim"] == "2500"


def test_collect_flags_count_mismatch(study):
    pmid, tag, d, _ = study
    stats = common.read_tsv(d / "ampliseq" / "dada2" / "DADA2_stats.tsv")
    stats[0]["nonchim"] = 1999
    common.write_tsv(d / "ampliseq" / "dada2" / "DADA2_stats.tsv", stats)
    integrity = collect.collect(pmid, tag, {"exit_code": 0})
    assert not integrity["pass"] and "abundance sum" in integrity["problems"][0]


def test_qc_gate_verdict(study):
    pmid, tag, _, _ = study
    collect.collect(pmid, tag, {"exit_code": 0})
    out = qc_gate.gate(pmid, tag)
    assert out["share_reads_unassigned_phylum"] == round(100 / 4500, 3)
    assert out["top_genera"][0]["genus"] == "Faecalibacterium"
    assert out["verdict"] == "pass", out["reasons"]


def test_cleanup_deletes_fastq_only_after_integrity_pass(study):
    pmid, tag, d, fq = study
    # no tables yet -> keep the reads
    with pytest.raises(run_study.StageError):
        run_study.stage_cleanup(pmid, tag, {"no_db": True})
    assert len(list(fq.glob("*.fastq.gz"))) == 4
    collect.collect(pmid, tag, {"exit_code": 0})
    run_study.stage_cleanup(pmid, tag, {"keep_fastq": True, "no_db": True})
    assert len(list(fq.glob("*.fastq.gz"))) == 4
    run_study.stage_cleanup(pmid, tag, {"no_db": True})
    assert list(fq.glob("*.fastq.gz")) == []
    assert json.loads((d / "tables" / "pipeline_run.json").read_text())["raw_fastq_deleted_at"]
    log = [json.loads(line) for line in (common.STUDIES / pmid / "log.jsonl").read_text().splitlines()]
    assert [e["outcome"] for e in log if e["action"] == "delete raw FASTQ"] == ["skipped", "skipped", "success"]


def test_parse_versions_yml(tmp_path):
    y = tmp_path / "nf_core_ampliseq_software_mqc_versions.yml"
    y.write_text('CUTADAPT_BASIC:\n  cutadapt: 5.2\nDADA2_DENOISING:\n  R: "4.5.2"\n  dada2: 1.38.0\nWorkflow:\n  Nextflow: 26.10.6\n')
    assert collect.parse_versions_yml(y) == {"CUTADAPT_BASIC": {"cutadapt": "5.2"},
                                              "DADA2_DENOISING": {"R": "4.5.2", "dada2": "1.38.0"},
                                              "Workflow": {"Nextflow": "26.10.6"}}


def test_as_int_reads_ampliseq_number_formats():
    assert collect.as_int("77,595") == 77595 and collect.as_int("97.7%") == 97
    assert collect.as_int("") is None and collect.as_int("NA") is None


def test_qc_gate_flags_runs_lost_in_quality_filtering(study):
    pmid, tag, d, _ = study
    common.write_tsv(d / "ampliseq" / "cutadapt" / "cutadapt_summary.tsv" if (d / "ampliseq" / "cutadapt").mkdir() is None else None,
                     [{"sample": "SRR1", "cutadapt_total_processed": "2,600", "cutadapt_passing_filters": "2,500",
                       "cutadapt_passing_filters_percent": "96.2%"},
                      {"sample": "SRR2", "cutadapt_total_processed": "80,000", "cutadapt_passing_filters": "78,000",
                       "cutadapt_passing_filters_percent": "97.5%"}])
    collect.collect(pmid, tag, {"exit_code": 0})
    qc = {r["run_accession"]: r for r in common.read_tsv(d / "tables" / "run_qc.tsv")}
    assert qc["SRR2"]["reads_input"] == "80000" and qc["SRR2"]["reads_after_trim"] == "78000"
    assert "kept only 4%" in qc["SRR2"]["failure_reason"]           # 2800 of 78000 survive filtering
    out = qc_gate.gate(pmid, tag)
    assert out["verdict"] == "review" and any("SRR2 4%" in r for r in out["reasons"])


def test_cleanup_rerun_keeps_first_deletion_time(study):
    pmid, tag, d, fq = study
    collect.collect(pmid, tag, {"exit_code": 0})
    run_study.stage_cleanup(pmid, tag, {"no_db": True})
    first = json.loads((d / "tables" / "pipeline_run.json").read_text())["raw_fastq_deleted_at"]
    collect.collect(pmid, tag, {"exit_code": 0})      # e.g. after a QC rule change
    run_study.stage_cleanup(pmid, tag, {"no_db": True})
    assert json.loads((d / "tables" / "pipeline_run.json").read_text())["raw_fastq_deleted_at"] == first
    log = [json.loads(line) for line in (common.STUDIES / pmid / "log.jsonl").read_text().splitlines()]
    assert sum(e["action"] == "delete raw FASTQ" for e in log) == 1
