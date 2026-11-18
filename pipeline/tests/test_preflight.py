import common
import make_samplesheet
import preflight


def test_revcomp_iupac():
    assert common.revcomp("ACGTN") == "NACGT"
    assert common.revcomp("GACTACHVGGGTATCTAATCC") == "GGATTAGATACCCBDGTAGTC"
    assert common.revcomp(common.revcomp("CCTACGGGNGGCWGCAG")) == "CCTACGGGNGGCWGCAG"


def test_find_primer_offset_and_mismatch():
    ps = preflight.primer_sets("CCTACGGGNGGCWGCAG")        # 341F, 17 nt -> 1 mismatch allowed
    read = "ACGT" + "CCTACGGGAGGCAGCAG" + "TGGGGAATATT"     # 4 nt spacer, exact match
    assert preflight.find_primer(read, ps, 0, preflight.MAX_OFFSET) == 4
    one_mm = "CCTACGGGAGGCAGCTG" + "TGGGG"
    assert preflight.find_primer(one_mm, ps, 0, preflight.MAX_OFFSET) == 0
    two_mm = "CCTTCGGGAGGCAGCTG" + "TGGGG"
    assert preflight.find_primer(two_mm, ps, 0, preflight.MAX_OFFSET) == -1
    assert preflight.find_primer("T" * 20 + "CCTACGGGAGGCAGCAG", ps, 0, preflight.MAX_OFFSET) == -1


def test_expected_amplicon_from_catalog():
    assert common.expected_amplicon_length("CCTACGGGNGGCWGCAG", "GACTACHVGGGTATCTAATCC") == 427   # 341F-785R
    assert common.expected_amplicon_length("GTGCCAGCMGCCGCGGTAA", "GGACTACHVGGGTWTCTAAT") == 253  # 515F-806R
    assert common.expected_amplicon_length("ACGCGHNRAACCTTACC", "ACGGGCRGTGWGTRCAA") == 404      # V6-V8
    assert common.expected_amplicon_length("AAAA", "GACTACHVGGGTATCTAATCC") is None


def test_batch_from_header():
    h = "@SRR15400632.1 M04386:115:000000000-C7H3B:1:1101:15589:1332 length=301"
    assert make_samplesheet.batch_from_header(h) == "M04386_115_000000000_C7H3B"
    assert make_samplesheet.batch_from_header("@SRR15400632.1 1 length=301") == "1"


def _quals(n, length, q="I", tail_q="#", tail_from=None):
    tail_from = length if tail_from is None else tail_from
    return [q * tail_from + tail_q * (length - tail_from) for _ in range(n)]


def test_truncation_keeps_auto_when_overlap_is_ample():
    q = _quals(200, 300)   # 2x300, all high quality
    t = preflight.decide_truncation(q, q, 17, 21, 427)
    assert t["params"] == {} and t["review"] == []


def test_truncation_set_explicitly_when_auto_would_break_merging():
    q1 = _quals(200, 250)
    q2 = _quals(200, 250, tail_from=200)   # reverse quality drops at 200 -> auto trunc ~179 after primer
    t = preflight.decide_truncation(q1, q2, 17, 21, 429)
    assert t["review"] == []
    assert t["params"]["trunclenf"] + t["params"]["trunclenr"] >= 429 + preflight.LENGTH_SLACK + preflight.MIN_OVERLAP


def test_truncation_review_when_reads_cannot_overlap():
    q = _quals(200, 150)
    t = preflight.decide_truncation(q, q, 17, 21, 427)
    assert t["params"] == {} and t["review"]


def _obs(**kw):
    base = {"run_accession": "R", "distinct_quality_values": 40, "fwd_offset_mode": 0, "rev_offset_mode": 0,
            "_qual_r1": _quals(50, 300), "_qual_r2": _quals(50, 300)}
    base.update(kw)
    return base


PE = {"platform": "ILLUMINA", "layout": "PAIRED", "forward_primer_seq": "CCTACGGGNGGCWGCAG",
      "reverse_primer_seq": "GACTACHVGGGTATCTAATCC"}


def test_decide_primers_present():
    r = preflight.decide(PE, [_obs(pct_fwd_at_r1_start=96, pct_rev_at_r1_start=0, pct_rev_at_r2_start=94)], [])
    assert r["decision"] == "run" and "skip_cutadapt" not in r["params"]
    assert r["observed"]["primers_in_reads"] is True
    assert r["params"]["exclude_taxa"] == "none" and r["params"]["skip_qiime"] is True


def test_decide_primers_absent_skips_cutadapt():
    r = preflight.decide(PE, [_obs(pct_fwd_at_r1_start=0.5, pct_rev_at_r1_start=0, pct_rev_at_r2_start=1)], [])
    assert r["decision"] == "run" and r["params"]["skip_cutadapt"] is True
    assert r["observed"]["primers_in_reads"] is False


def test_decide_partial_primers_needs_review():
    r = preflight.decide(PE, [_obs(pct_fwd_at_r1_start=50, pct_rev_at_r1_start=0, pct_rev_at_r2_start=50)], [])
    assert r["decision"] == "needs_review"


def test_decide_454_reverse_orientation():
    se454 = {"platform": "LS454", "layout": "SINGLE", "forward_primer_seq": "CCTACGGGAGGCAGCAG",
             "reverse_primer_seq": "CCGTCAATTCMTTTRAGT"}
    r = preflight.decide(se454, [_obs(pct_fwd_at_r1_start=0, pct_rev_at_r1_start=92, pct_fwd_rc_at_r1_end=10,
                                      pct_rev_rc_at_r1_end=0)], [])
    assert r["decision"] == "run"
    assert r["observed"]["orientation"] == "reverse"
    assert r["params"]["primer_fwd"] == "CCGTCAATTCMTTTRAGT" and r["params"]["dada_taxonomy_rc"] is True
    assert r["params"]["sequencing_type"] == "illumina_se" and r["observed"]["dada2_homopolymer_settings"]


def test_balanced_subset():
    runs = [{"run_accession": f"R{i}", "group": "case" if i % 3 else "control"} for i in range(12)]
    sub = make_samplesheet.balanced_subset(runs, 6)
    assert len(sub) == 6 and sum(r["group"] == "control" for r in sub) == 3


def test_partial_primer_at_read_start():
    ps = preflight.primer_sets("CCTAYGGGRBGCASCAG")           # Novogene 341F
    assert preflight.partial_primer("TACGGGAGGCAGCAGTGGGG", ps) == 15   # first 2 bases clipped
    assert preflight.partial_primer("GGAGGCAGCAGTGGGGAATA", ps) == 11
    assert preflight.partial_primer("TGGGGAATATTGCACAATGG", ps) == 0
    pct, _ = preflight.starts_with(["TACGGGAGGCAGCAGTGGGG", "TCCTACGGGAGGCAGCAGTG", "TGGGGAATATTGCACAATGG"], ps)
    assert pct == 66.7


def test_orientation_votes_from_conserved_sites():
    cat = common.load_primer_catalog()
    gene = "AAAA" + "GTGCCAGCAGCCGCGGTAA" + "TACGGAGGGTGCAAGCG"      # 515F site in gene orientation
    rev = common.revcomp(gene)
    v = preflight.orientation_votes([gene, gene, rev, "ACGT" * 10], cat)
    assert v == {"gene": 50.0, "reverse": 25.0}


def test_decide_454_trimmed_reverse_reads_with_lost_quality():
    se454 = {"platform": "LS454", "layout": "SINGLE", "forward_primer_seq": "CCTACGGGAGGCAGCAG",
             "reverse_primer_seq": "CCGTCAATTCMTTTRAGT"}
    o = _obs(pct_fwd_at_r1_start=0, pct_rev_at_r1_start=0, pct_fwd_rc_at_r1_end=0, pct_rev_rc_at_r1_end=0,
             orientation_r1={"gene": 0.0, "reverse": 88.3}, distinct_quality_values=2)
    r = preflight.decide(se454, [o], [])
    assert r["observed"]["orientation"] == "reverse" and r["params"]["skip_cutadapt"] is True
    assert r["params"]["dada_taxonomy_rc"] is True
    assert r["decision"] == "needs_review" and "carry no information" in r["review_reasons"][0]


def test_empty_mate_file_is_an_error_not_zero_percent(monkeypatch):
    """E22: ENA served empty bodies; preflight must not read that as 'no primers'."""
    reads = [("@r1", "CCTACGGGAGGCAGCAG" + "A" * 200, "I" * 217)] * 5
    monkeypatch.setattr(preflight, "stream_reads", lambda url, *a, **k: [] if url.endswith("_2.fastq.gz") else reads)
    run = {"run_accession": "R", "fastq_urls": "x/R_1.fastq.gz;x/R_2.fastq.gz"}
    try:
        preflight.observe_run(run, PE, [])
    except RuntimeError as e:
        assert "R2 file gave no reads" in str(e)
    else:
        raise AssertionError("an empty R2 file must raise")


def test_decide_paired_without_r2_observations_needs_review():
    """A paired protocol whose observations lack R2 (e.g. a single dumped file) goes to review, no crash."""
    r = preflight.decide(PE, [_obs(pct_fwd_at_r1_start=99, pct_rev_at_r1_start=0)], [])
    assert r["decision"] == "needs_review" and "R2" in r["review_reasons"][0]


def test_catalog_matches_a_primer_printed_one_base_short():
    """36111238 prints 338F without its last base; the amplicon length must still be known."""
    assert common.catalog_by_seq("ACTCCTACGGGAGGCAGCA")["name"] == "338F"
    assert common.expected_amplicon_length("ACTCCTACGGGAGGCAGCA", "GGACTACHVGGGTWTCTAAT") == 429
    assert common.catalog_by_seq("ACTCCTACGGGAGG") is None  # far shorter: no guess


def test_run_read_lengths_keep_only_fixed_length_runs():
    m = [{"run_accession": "A", "status": "ok", "read_count": "100", "base_count": "60200"},   # 2 x 301
         {"run_accession": "B", "status": "ok", "read_count": "100", "base_count": "60000"},   # 2 x 300
         {"run_accession": "C", "status": "ok", "read_count": "100", "base_count": "59731"},   # trimmed
         {"run_accession": "D", "status": "no_fastq", "read_count": "100", "base_count": "60200"}]
    assert preflight.run_read_lengths(m, paired=True) == {"A": 301, "B": 300}


def test_shortest_batch_ignores_a_lone_short_run():
    lengths = {f"r{i}": 301 for i in range(40)} | {"odd": 250}
    assert preflight.shortest_batch(lengths)["length"] == 301
    lengths |= {f"s{i}": 300 for i in range(14)}
    b = preflight.shortest_batch(lengths)
    assert b["length"] == 300 and b["n_runs"] == 14


def test_truncation_capped_when_a_batch_has_shorter_reads():
    """36111238: 14 of 60 runs are 2x300 among 2x301; ampliseq's 274 dropped 98 % of their reads."""
    q = _quals(50, 301)
    batch = {"length": 300, "n_runs": 14, "runs": ["SRR1"], "lengths": {300: 14, 301: 46}}
    c = preflight.cap_truncation({}, q, q, 27, 28, 429, batch)
    assert c["params"]["trunclenf"] <= 300 - 27 - preflight.CAP_MARGIN
    assert c["params"]["trunclenr"] <= 300 - 28 - preflight.CAP_MARGIN
    assert not c["review"]


def test_truncation_untouched_when_all_runs_share_one_length():
    q = _quals(50, 301)
    batch = {"length": 301, "n_runs": 60, "runs": ["SRR1"], "lengths": {301: 60}}
    assert preflight.cap_truncation({}, q, q, 27, 28, 429, batch)["params"] == {}
    assert preflight.cap_truncation({}, q, q, 27, 28, 429, None)["params"] == {}


def test_truncation_cap_that_breaks_merging_goes_to_review():
    q = _quals(50, 301)
    batch = {"length": 200, "n_runs": 10, "runs": ["SRR1"], "lengths": {200: 10, 301: 50}}
    c = preflight.cap_truncation({}, q, q, 27, 28, 429, batch)
    assert c["params"] == {} and "too little overlap" in c["review"][0]


def test_quality_fallback_dumps_paired_runs_as_r1_and_r2(tmp_path, monkeypatch):
    """37696680 (E22): ENA FASTQ from SRA Lite on a paired study; the NCBI copy must come as two files."""
    seen = {}
    monkeypatch.setattr(preflight.sra_source, "have_tools", lambda: True)
    monkeypatch.setattr(preflight.sra_source, "fetch_split", lambda acc, outdir, cache: {
        "run": acc, "paths": [f"{acc}_1.fastq.gz", f"{acc}_2.fastq.gz"], "sra_bytes": 10, "reads": 5})
    monkeypatch.setattr(preflight.sra_source, "fetch",
                        lambda *a: (_ for _ in ()).throw(AssertionError("single-file dump used for paired data")))

    def fake_observe(run, protocol, catalog):
        seen[run["run_accession"]] = run["fastq_urls"]
        return {"run_accession": run["run_accession"], "n_reads": 5, "top_r1_starts": {"ACTC": 5},
                "distinct_quality_values": 38}
    monkeypatch.setattr(preflight, "observe_run", fake_observe)
    monkeypatch.setattr(preflight.common, "data_dir", lambda: tmp_path)
    ena = [{"run_accession": "SRR9", "n_reads": 5, "top_r1_starts": {"ACTC": 5}, "distinct_quality_values": 2}]
    obs, errors, note = preflight.quality_fallback("1", "main", ena, [], PE, [])
    assert note["used"] and seen == {"SRR9": "SRR9_1.fastq.gz;SRR9_2.fastq.gz"}
