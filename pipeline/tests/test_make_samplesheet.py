"""make_samplesheet.resolve_layout: ENA's declared layout against the files it serves (36111238)."""
import make_samplesheet as ms

R1 = "ftp.sra.ebi.ac.uk/vol1/fastq/SRR210/085/SRR21032385/SRR21032385_1.fastq.gz"
R2 = "ftp.sra.ebi.ac.uk/vol1/fastq/SRR210/085/SRR21032385/SRR21032385_2.fastq.gz"
SE = "ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/SRR1234567/SRR1234567.fastq.gz"


def run(layout, *urls):
    return {"run_accession": "SRR21032385", "library_layout": layout, "fastq_ftp": ";".join(urls)}


def test_single_in_ena_with_mate_files_and_paired_protocol_is_processed_paired():
    r = run("SINGLE", R1, R2)
    layout, note = ms.resolve_layout(r, {"layout": "PAIRED"})
    assert layout == "PAIRED" and note.startswith("layout corrected")
    r["library_layout"] = layout
    assert ms.pick_files(r) == [R1, R2]


def test_single_in_ena_with_mate_files_without_protocol_support_is_flagged_not_guessed():
    layout, note = ms.resolve_layout(run("SINGLE", R1, R2), {"layout": "SINGLE"})
    assert layout == "SINGLE" and "does not say PAIRED" in note
    layout, note = ms.resolve_layout(run("SINGLE", R1, R2), {})
    assert layout == "SINGLE" and note


def test_ordinary_runs_are_left_alone():
    assert ms.resolve_layout(run("PAIRED", R1, R2), {"layout": "PAIRED"}) == ("PAIRED", "")
    assert ms.resolve_layout(run("SINGLE", SE), {"layout": "SINGLE"}) == ("SINGLE", "")
    # a single file stays single even if the paper says paired: there is no R2 to use
    assert ms.resolve_layout(run("SINGLE", SE), {"layout": "PAIRED"}) == ("SINGLE", "")
    # PAIRED with ENA's extra orphan-reads file still picks the two mates
    orphan = SE.replace("SRR1234567.fastq.gz", "SRR21032385.fastq.gz")
    r = run("PAIRED", orphan, R1, R2)
    assert ms.resolve_layout(r, {"layout": "PAIRED"}) == ("PAIRED", "")
    assert ms.pick_files(r) == [R1, R2]
