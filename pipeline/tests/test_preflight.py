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
