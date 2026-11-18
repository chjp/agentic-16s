"""Task 3.1b: look at the reads before downloading them, and choose ampliseq parameters.

For a handful of runs per protocol, stream the first reads straight from ENA (a few MB) and check
what the paper (or the LLM) claimed against what is actually in the reads:

  * are the stated primers at the start of the reads (or already trimmed off)?
  * which catalog primers are there, if the paper gave none?
  * read orientation (single-end reads that start with the reverse primer)
  * read lengths and quality, binned quality scores
  * whether paired reads can still overlap after truncation

Writes preflight.json (per-run observations) and params.json (ampliseq parameters, each
non-default one with the reason it was set). decision = "run" or "needs_review".
"""
from __future__ import annotations

import argparse
import sys
import datetime as dt
import gzip
import json
import statistics
import time
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules; python3 -I drops this
import common  # noqa: E402
import sra_source  # noqa: E402

READS_PER_RUN = 2000
RUNS_TO_SAMPLE = 8
MAX_OFFSET = 12          # primer may start up to this many bases into the read (spacers, barcodes)
PRESENT_PCT = 80.0       # primers found in at least this share of reads -> trim them
ABSENT_PCT = 5.0         # primers found in fewer than this share -> reads were already trimmed
MIXED_PCT = 20.0         # reverse primer at the start of R1 in this share -> mixed orientation
QMIN = 25                # same default as ampliseq --trunc_qmin
RMIN = 0.75              # same default as ampliseq --trunc_rmin
MIN_OVERLAP = 12         # DADA2 mergePairs minOverlap used by ampliseq
LENGTH_SLACK = 20        # amplicons of real taxa can be longer than the E. coli length

# Settings that are the same for every study. Values here are part of params.json.
FIXED_PARAMS = {
    "skip_qiime": (True, "QIIME2 steps are not needed: diversity and statistics run on the database. Saves the large QIIME2 image."),
    "exclude_taxa": ("none", "Store everything: keep mitochondria/chloroplast ASVs and filter in analysis views, so counts add up to DADA2's non-chimeric reads."),
    "ignore_failed_trimming": (True, "One run with too few reads must not stop the study; it is recorded as failed in run_qc."),
    "ignore_empty_input_files": (True, "Same as above for empty FASTQ files."),
    "ignore_failed_filtering": (True, "Same as above for runs that lose all reads in quality filtering."),
    "seed": (100, "Pinned random seed (ampliseq default) so reruns are reproducible."),
    "skip_dada_addspecies": (True, "Owner's decision 2026-11-16: species assignment (dada2 addSpecies, exact matching against "
                                   "1.47 M SILVA species references) was the slowest step of the GCE batch: single-threaded, "
                                   "15-16 GB per 2000-ASV chunk, about 45 min each, so the chunks could not run in parallel "
                                   "(about 5 h for 37661264). Short V3-V4/V4 reads rarely resolve species anyway, and genus "
                                   "level is enough to separate disease from healthy. Studies run before this date have species."),
    "sample_inference": ("pseudo", "Pseudo-pooling (owner's decision 2026-11-15): ampliseq's default 'pooled' denoised 37661264's "
                                   "97 runs as one job, about 1 h 45 min per read direction on 3 of 8 vCPUs. Pseudo denoises "
                                   "each run on its own, twice, sharing the variants found in the first pass, so rare variants "
                                   "are still kept and every core is used. Studies run before this date used 'pooled'."),
    "skip_phyloseq": (True, "R objects are not needed (the database tables are the output); their conda environment does not solve on osx-arm64."),
    "skip_report": (True, "The R Markdown summary report is not needed (MultiQC plus pipeline_qc.json cover QC); its pinned R 4.2.3 environment is broken on osx-arm64 (rlang.dylib missing)."),
    "skip_tse": (True, "Same as skip_phyloseq: TreeSummarizedExperiment 2.10.0 needs a BiocParallel build missing for osx-arm64."),
}


# ---------------------------------------------------------------- reading

def https_url(ftp_path: str) -> str:
    return ftp_path if ftp_path.startswith("http") else "https://" + ftp_path


def stream_reads(url: str, n: int = READS_PER_RUN, attempts: int = 3) -> list[tuple[str, str, str]]:
    """First n FASTQ records of a remote .fastq.gz without downloading the whole file.
    ENA sometimes answers with an HTML error page under load; retry those."""
    for i in range(attempts):
        try:
            return _stream_reads(url, n)
        except (OSError, EOFError):
            if i == attempts - 1:
                raise
            time.sleep(10 * (i + 1))
    return []


def _stream_reads(url: str, n: int) -> list[tuple[str, str, str]]:
    out = []
    if Path(url).exists():  # a local file (FASTQ dumped from NCBI's .sra)
        resp_ctx = open(url, "rb")
    else:
        resp_ctx = urllib.request.urlopen(urllib.request.Request(https_url(url), headers={"User-Agent": "agentic-16s"}),
                                          timeout=120)
    with resp_ctx as resp, gzip.GzipFile(fileobj=resp) as gz:
        while len(out) < n:
            h = gz.readline()
            if not h:
                break
            s = gz.readline().strip().decode()
            gz.readline()
            q = gz.readline().strip().decode()
            out.append((h.strip().decode(), s.upper(), q))
    return out


# ---------------------------------------------------------------- primer matching

def primer_sets(seq: str) -> list[frozenset]:
    return [frozenset(common.IUPAC[b] + "N") for b in seq.upper()]


def find_primer(read: str, psets: list[frozenset], start: int = 0, stop: int | None = None,
                max_mm: int | None = None) -> int:
    """Offset of the first match of the primer starting in [start, stop], else -1.
    Allows about 10 % mismatches, like Cutadapt's default error rate."""
    L = len(psets)
    if max_mm is None:
        max_mm = int(0.1 * L)
    last = len(read) - L
    stop = last if stop is None else min(stop, last)
    for off in range(max(0, start), stop + 1):
        mm = 0
        for j, allowed in enumerate(psets):
            if read[off + j] not in allowed:
                mm += 1
                if mm > max_mm:
                    break
        else:
            return off
    return -1


def pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


PARTIAL_MIN = 10   # a read may start with only the 3' part of the primer (5' end clipped)


def partial_primer(read: str, psets: list[frozenset], min_len: int = PARTIAL_MIN) -> int:
    """Length of the longest primer suffix the read starts with, else 0. Cutadapt's non-anchored
    5' adapters (-g) trim these too, so they count as primer found."""
    for k in range(len(psets) - 1, min_len - 1, -1):
        if len(read) < k:
            continue
        mm, max_mm = 0, int(0.1 * k)
        for j, allowed in enumerate(psets[-k:]):
            if read[j] not in allowed:
                mm += 1
                if mm > max_mm:
                    break
        else:
            return k
    return 0


def starts_with(reads: list[str], psets) -> tuple[float, list[int]]:
    """Share of reads with the primer at the start (full within MAX_OFFSET, or a clipped suffix).
    Also returns the offsets of the full matches."""
    hits, n = [], 0
    for r in reads:
        o = find_primer(r, psets, 0, MAX_OFFSET)
        if o >= 0:
            hits.append(o)
            n += 1
        elif partial_primer(r, psets):
            n += 1
    return pct(n, len(reads)), hits


def orientation_votes(reads: list[str], catalog: list[dict]) -> dict:
    """Read orientation from conserved 16S sites (the catalog primer sites) anywhere in the read:
    a fwd primer site, or the reverse complement of a rev primer site, means gene orientation."""
    gene = [common.primer_regex(c["sequence"] if c["direction"] == "fwd" else common.revcomp(c["sequence"])) for c in catalog]
    anti = [common.primer_regex(common.revcomp(c["sequence"]) if c["direction"] == "fwd" else c["sequence"]) for c in catalog]
    g = a = 0
    for r in reads:
        is_g = any(p.search(r) for p in gene)
        is_a = any(p.search(r) for p in anti)
        g += is_g and not is_a
        a += is_a and not is_g
    return {"gene": pct(g, len(reads)), "reverse": pct(a, len(reads))}


def ends_with(reads: list[str], psets, window: int = 80) -> float:
    """Share of reads that contain the primer in their last `window` bases (full-length reads)."""
    n = sum(1 for r in reads if find_primer(r, psets, max(0, len(r) - window)) >= 0)
    return pct(n, len(reads))


# ---------------------------------------------------------------- per-run observation

def quantiles(xs: list[int]) -> dict:
    if not xs:
        return {}
    s = sorted(xs)
    q = lambda f: s[min(len(s) - 1, int(f * (len(s) - 1)))]  # noqa: E731
    return {"min": s[0], "p10": q(0.10), "p25": q(0.25), "median": q(0.5), "max": s[-1]}


def observe_run(run: dict, protocol: dict, catalog: list[dict]) -> dict:
    urls = run["fastq_urls"].split(";")
    r1 = stream_reads(urls[0])
    r2 = stream_reads(urls[1]) if len(urls) > 1 else []
    # An empty answer is not "0 % primers": ENA sometimes serves empty bodies under load (E22).
    for name, reads, url in (("R1", r1, urls[0]), ("R2", r2, urls[1] if len(urls) > 1 else None)):
        if url and not reads:
            raise RuntimeError(f"{name} file gave no reads: {url}")
    s1 = [s for _, s, _ in r1]
    s2 = [s for _, s, _ in r2]
    obs = {"run_accession": run["run_accession"], "n_reads": len(r1),
           "first_header": r1[0][0] if r1 else "",
           "len_r1": quantiles([len(s) for s in s1]), "len_r2": quantiles([len(s) for s in s2])}
    fwd, rev = protocol.get("forward_primer_seq"), protocol.get("reverse_primer_seq")
    if fwd and rev:
        pf, pr = primer_sets(fwd), primer_sets(rev)
        obs["pct_fwd_at_r1_start"], offs = starts_with(s1, pf)
        obs["pct_rev_at_r1_start"], _ = starts_with(s1, pr)
        obs["fwd_offset_mode"] = Counter(offs).most_common(1)[0][0] if offs else None
        if s2:
            obs["pct_rev_at_r2_start"], offs2 = starts_with(s2, pr)
            obs["pct_fwd_at_r2_start"], _ = starts_with(s2, pf)
            obs["rev_offset_mode"] = Counter(offs2).most_common(1)[0][0] if offs2 else None
        else:
            obs["pct_rev_rc_at_r1_end"] = ends_with(s1, primer_sets(common.revcomp(rev)))
            obs["pct_fwd_rc_at_r1_end"] = ends_with(s1, primer_sets(common.revcomp(fwd)))
    # catalog scan: which known primer sits at the start of R1 / R2?
    for key, reads in (("catalog_r1", s1), ("catalog_r2", s2)):
        if not reads:
            continue
        hits = {c["name"]: starts_with(reads[:500], primer_sets(c["sequence"]))[0] for c in catalog}
        obs[key] = dict(sorted(hits.items(), key=lambda kv: -kv[1])[:3])
    obs["orientation_r1"] = orientation_votes(s1[:500], catalog)
    obs["top_r1_starts"] = dict(Counter(s[:8] for s in s1).most_common(5))
    obs["distinct_quality_values"] = len({c for _, _, q in r1 + r2 for c in q})
    obs["_qual_r1"] = [q for _, _, q in r1]
    obs["_qual_r2"] = [q for _, _, q in r2]
    return obs


# ---------------------------------------------------------------- decisions

def median_quality_profile(quals: list[str]) -> list[float]:
    """Median Phred score at each position, over reads long enough to have that position."""
    if not quals:
        return []
    maxlen = max(len(q) for q in quals)
    prof = []
    for i in range(maxlen):
        col = [ord(q[i]) - 33 for q in quals if len(q) > i]
        if len(col) < max(10, 0.05 * len(quals)):
            break
        prof.append(statistics.median(col))
    return prof


def auto_trunc(quals: list[str], trim: int) -> tuple[int, int]:
    """Predict ampliseq's automatic truncation on primer-trimmed reads:
    (quality-based length, length that RMIN of reads still reach)."""
    prof = median_quality_profile(quals)[trim:]
    q_len = next((i for i, q in enumerate(prof) if q < QMIN), len(prof))
    lens = sorted(max(0, len(q) - trim) for q in quals)
    l_len = lens[int((1 - RMIN) * (len(lens) - 1))] if lens else 0
    return q_len, l_len


def decide_truncation(q1: list[str], q2: list[str], trim1: int, trim2: int, amplicon: int | None) -> dict:
    """Paired reads only. Keep ampliseq's automatic truncation unless it would break merging."""
    if amplicon is None:
        return {"params": {}, "reasons": {}, "review": [],
                "note": "expected amplicon length unknown (primers not in catalog); merge overlap not checked"}
    need = amplicon + LENGTH_SLACK + MIN_OVERLAP
    qf, lf = auto_trunc(q1, trim1)
    qr, lr = auto_trunc(q2, trim2)
    auto_f, auto_r = min(qf, lf), min(qr, lr)
    info = {"expected_amplicon": amplicon, "needed_r1_plus_r2": need, "auto_f": auto_f, "auto_r": auto_r,
            "max_f": lf, "max_r": lr, "predicted_auto_overlap": auto_f + auto_r - amplicon}
    if auto_f + auto_r >= need:
        return {"params": {}, "reasons": {}, "review": [], "note": info}
    if lf + lr < need:
        return {"params": {}, "reasons": {}, "note": info,
                "review": [f"reads too short to merge: R1+R2 after trimming reach {lf + lr} nt, "
                           f"amplicon {amplicon} nt needs {need}; consider forward reads only or mergepairs_strategy consensus"]}
    tf = lf
    tr = min(lr, max(auto_r, need - tf))
    reason = (f"ampliseq's automatic truncation ({auto_f}/{auto_r}) would leave {auto_f + auto_r - amplicon} nt "
              f"overlap for a ~{amplicon} nt amplicon; truncate at {tf}/{tr} to keep >= {MIN_OVERLAP + LENGTH_SLACK} nt")
    return {"params": {"trunclenf": tf, "trunclenr": tr},
            "reasons": {"trunclenf": reason, "trunclenr": reason}, "review": [], "note": info}


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def decide(protocol: dict, runs_obs: list[dict], catalog: list[dict]) -> dict:
    platform = protocol.get("platform") or ""
    paired = protocol.get("layout") == "PAIRED"
    fwd, rev = protocol.get("forward_primer_seq"), protocol.get("reverse_primer_seq")
    params, reasons, review = {}, {}, []
    observed: dict = {"primer_source": "paper" if fwd and rev else None}

    # 1. primers stated? If not, adopt the catalog pair that is clearly in the reads.
    if not (fwd and rev):
        best1 = Counter()
        best2 = Counter()
        for o in runs_obs:
            for k, v in (o.get("catalog_r1") or {}).items():
                best1[k] += v / len(runs_obs)
            for k, v in (o.get("catalog_r2") or {}).items():
                best2[k] += v / len(runs_obs)
        cat = {c["name"]: c for c in catalog}
        f_name = next((k for k, v in best1.most_common() if cat[k]["direction"] == "fwd"), None)
        r_name = next((k for k, v in best2.most_common() if cat[k]["direction"] == "rev"), None)
        if f_name and r_name and best1[f_name] >= PRESENT_PCT and (not paired or best2[r_name] >= PRESENT_PCT):
            fwd, rev = cat[f_name]["sequence"], cat[r_name]["sequence"]
            observed.update(primer_source=f"inferred:preflight catalog match {f_name}/{r_name}",
                            forward_primer_name=f_name, reverse_primer_name=r_name)
            protocol = {**protocol, "forward_primer_seq": fwd, "reverse_primer_seq": rev}
            runs_obs = [observe_primers_again(o, fwd, rev) for o in runs_obs]
        else:
            review.append("paper gives no primers and no catalog primer pair is clearly at the start of the reads "
                          f"(best R1 {f_name} {best1[f_name] if f_name else 0:.0f}%, best R2 {r_name} {best2[r_name] if r_name else 0:.0f}%)")
            return finish(params, reasons, review, observed, runs_obs)

    f1 = med([o.get("pct_fwd_at_r1_start") for o in runs_obs])
    r1 = med([o.get("pct_rev_at_r1_start") for o in runs_obs])
    r2 = med([o.get("pct_rev_at_r2_start") for o in runs_obs]) if paired else None
    observed.update(pct_fwd_at_r1_start=f1, pct_rev_at_r1_start=r1, pct_rev_at_r2_start=r2)
    params["primer_fwd"], params["primer_rev"] = fwd, rev
    reasons["primer_fwd"] = reasons["primer_rev"] = f"primers from {observed['primer_source']}"
    orientation = "forward"
    amplicon = common.expected_amplicon_length(fwd, rev)
    observed["expected_amplicon_length"] = amplicon

    # 2. sequencing type and primer presence
    votes = {k: med([(o.get("orientation_r1") or {}).get(k) for o in runs_obs]) or 0.0 for k in ("gene", "reverse")}
    observed["orientation_votes_r1"] = votes
    if f1 is None or (paired and r2 is None):
        review.append("no primer observations for " + ("R1" if f1 is None else "R2") +
                      f" in any sampled run (layout {protocol.get('layout')}): the reads could not be read as expected")
        return finish(params, reasons, review, observed, runs_obs)
    if paired:
        params["sequencing_type"] = "illumina_pe"
        present = min(f1, r2) >= PRESENT_PCT
        absent = max(f1, r2) < ABSENT_PCT
        if r1 >= MIXED_PCT or votes["reverse"] >= MIXED_PCT:
            review.append(f"mixed read orientation: reverse primer starts {r1}% of R1 reads; "
                          f"conserved sites put {votes['reverse']}% of R1 reads in reverse orientation")
    else:
        if f1 >= PRESENT_PCT or (f1 >= r1 and f1 >= ABSENT_PCT):
            present, absent = f1 >= PRESENT_PCT, False
        elif r1 >= ABSENT_PCT:  # reads start with the reverse primer: sequenced from the 3' end of the amplicon
            orientation = "reverse"
            present, absent = r1 >= PRESENT_PCT, False
        else:  # no primer at the start: orientation from conserved 16S sites inside the reads
            present, absent = False, True
            if votes["reverse"] >= 50 and votes["reverse"] > 4 * votes["gene"]:
                orientation = "reverse"
        if platform in ("LS454", "ION_TORRENT"):
            full = med([o.get("pct_fwd_rc_at_r1_end" if orientation == "reverse" else "pct_rev_rc_at_r1_end") for o in runs_obs])
            observed["pct_full_length_reads"] = full
            if present and full is not None and full >= PRESENT_PCT and orientation == "forward":
                params["sequencing_type"] = "iontorrent"
                reasons["sequencing_type"] = (f"{platform}: {full}% of reads span both primers; ampliseq's iontorrent mode "
                                              "trims both and uses DADA2 homopolymer settings")
            else:
                params["sequencing_type"] = "illumina_se"
                reasons["sequencing_type"] = (f"{platform}: only {full}% of reads reach the far primer, so ampliseq's "
                                              "iontorrent mode (both primers required) would discard most reads; single-end "
                                              "trimming of the 5' primer, with DADA2 homopolymer settings from extra.config")
                observed["dada2_homopolymer_settings"] = True
                # Variable-length reads: without a fixed length, one organism becomes several ASVs of different
                # lengths, and short artefacts survive (minLen 50). Truncate to the length RMIN of reads reach,
                # capped by quality, as ampliseq's automatic truncation would (it left truncLen = 0 for 454 SE).
                q1 = [q for o in runs_obs for q in o["_qual_r1"]]
                q_len, l_len = auto_trunc(q1, 0)
                tl = min(q_len, l_len) // 10 * 10
                if tl >= 150:
                    params["trunclenf"] = tl
                    reasons["trunclenf"] = (f"{platform} reads vary in length; {RMIN:.0%} of reads reach {l_len} nt and "
                                            f"median quality stays >= {QMIN} up to {q_len} nt, so reads are truncated to "
                                            f"{tl} nt (shorter reads are dropped) to give one length per ASV")
                    observed["expected_asv_length"] = tl
                else:
                    review.append(f"{platform} reads too short or poor for a fixed truncation length "
                                  f"({RMIN:.0%} reach {l_len} nt, quality >= {QMIN} up to {q_len} nt)")
        else:
            params["sequencing_type"] = "illumina_se"
    if orientation == "reverse":
        params["primer_fwd"], params["primer_rev"] = rev, fwd
        why = (f"reads start with the reverse primer ({r1}% of reads)" if r1 >= ABSENT_PCT else
               f"reads carry no primer, and conserved 16S sites put {votes['reverse']}% of reads in reverse orientation")
        reasons["primer_fwd"] = reasons["primer_rev"] = (
            f"{why}, so the reverse primer is the 5' primer; "
            "ASVs come out reverse-complemented and are flipped back by collect.py")
        params["dada_taxonomy_rc"] = True
        reasons["dada_taxonomy_rc"] = "ASVs are in reverse orientation; let DADA2 classify the reverse complement"
    observed["orientation"] = orientation

    found = r1 if orientation == "reverse" else f1
    if present:
        observed["primers_in_reads"] = True
    elif absent:
        observed["primers_in_reads"] = False
        params["skip_cutadapt"] = True
        reasons["skip_cutadapt"] = (f"primers found at the start of only {found}% of reads "
                                    f"(R2 {r2}%): the archive reads were already trimmed")
    else:
        observed["primers_in_reads"] = None
        review.append(f"primers found in {found}% of R1 starts (R2: {r2}%): neither clearly present nor absent")

    # 3. merge overlap (paired only)
    if paired and not review:
        trim1 = 0 if params.get("skip_cutadapt") else len(fwd) + (med([o.get("fwd_offset_mode") for o in runs_obs]) or 0)
        trim2 = 0 if params.get("skip_cutadapt") else len(rev) + (med([o.get("rev_offset_mode") for o in runs_obs]) or 0)
        q1 = [q for o in runs_obs for q in o["_qual_r1"]]
        q2 = [q for o in runs_obs for q in o["_qual_r2"]]
        t = decide_truncation(q1, q2, int(trim1), int(trim2), amplicon)
        params.update(t["params"])
        reasons.update(t["reasons"])
        review += t["review"]
        observed["truncation"] = t["note"]

    # 4. binned quality scores (NovaSeq, newer MiSeq software)
    dq = min(o["distinct_quality_values"] for o in runs_obs)
    observed["distinct_quality_values"] = dq
    if dq <= 2:
        review.append(f"quality scores carry no information ({dq} distinct values; the archive replaced them), "
                      "so DADA2's error model cannot use them: decide whether to denoise this study at all")
    elif dq <= 7:
        review.append(f"quality scores look binned ({dq} distinct values); set --binned_quality for the instrument")
    return finish(params, reasons, review, observed, runs_obs)


def observe_primers_again(o: dict, fwd: str, rev: str) -> dict:
    """The paper gave no primers, so the per-primer percentages come from the catalog scan
    (same matcher, first 500 reads of each run) instead of a second download."""
    o = dict(o)
    names = {c["sequence"]: c["name"] for c in common.load_primer_catalog()}
    o["pct_fwd_at_r1_start"] = (o.get("catalog_r1") or {}).get(names[fwd], 0.0)
    o["pct_rev_at_r2_start"] = (o.get("catalog_r2") or {}).get(names[rev], 0.0)
    o["pct_rev_at_r1_start"] = (o.get("catalog_r1") or {}).get(names[rev], 0.0)
    o.setdefault("fwd_offset_mode", 0)
    o.setdefault("rev_offset_mode", 0)
    return o


def finish(params, reasons, review, observed, runs_obs) -> dict:
    params.setdefault("dada_ref_taxonomy", common.versions().get("DADA_REF_TAXONOMY", "silva=144"))
    reasons.setdefault("dada_ref_taxonomy", "Owner chose SILVA only (2026-11-14): genus names match most IBD papers; "
                                            "GTDB can be added later as extra taxonomy_assignment rows")
    for k, (v, why) in FIXED_PARAMS.items():
        params.setdefault(k, v)
        reasons.setdefault(k, why)
    return {"decision": "needs_review" if review else "run", "review_reasons": review,
            "params": params, "reasons": reasons, "observed": observed}


# ---------------------------------------------------------------- driver

def quality_fallback(pmid, label, obs, errors, protocol, catalog):
    """ENA's FASTQ lost the quality scores: look for NCBI's normalized .sra of the sampled runs.

    The sampled runs are fetched into the study's FASTQ folder, where the fetch stage reuses them.
    Returns (observations, errors, note); note["used"] is True only if every sampled run has a copy
    with real quality scores and the same reads as ENA."""
    dq_ena = min(o["distinct_quality_values"] for o in obs)
    if not sra_source.have_tools():
        return obs, errors, {"used": False, "ena_distinct_quality_values": dq_ena,
                             "reason": "sra-tools not installed, so NCBI's .sra could not be checked"}
    outdir = common.data_dir() / "fastq" / pmid / label / "fastq"
    cache = common.STUDIES / pmid / "sources" / "archive"
    new_obs, new_err, fetched = [], [], []
    for o in obs:
        acc = o["run_accession"]
        try:
            f = sra_source.fetch(acc, outdir, cache)
            no = observe_run({"run_accession": acc, "fastq_urls": f["path"]}, protocol, catalog)
            if no["n_reads"] != o["n_reads"] or no["top_r1_starts"] != o["top_r1_starts"]:
                raise RuntimeError("reads differ from ENA's FASTQ")
            new_obs.append(no)
            fetched.append(f)
        except Exception as e:  # noqa: BLE001 - recorded; the study then stays at needs_review
            new_err.append(f"{acc}: {e}")
    dq_sra = min((o["distinct_quality_values"] for o in new_obs), default=0)
    if new_err or dq_sra <= 2:
        why = "; ".join(new_err) or f"NCBI's .sra also has only {dq_sra} distinct quality values"
        return obs, errors, {"used": False, "ena_distinct_quality_values": dq_ena, "reason": why}
    return new_obs, errors, {
        "used": True, "ena_distinct_quality_values": dq_ena, "sra_distinct_quality_values": dq_sra,
        "runs_checked": len(new_obs), "sra_bytes": sum(f["sra_bytes"] or 0 for f in fetched),
        "reason": (f"ENA's FASTQ has {dq_ena} distinct quality values (derived from SRA Lite); NCBI's normalized .sra "
                   f"has {dq_sra}, with the same reads in {len(new_obs)} sampled runs, so every run is fetched from "
                   "NCBI (md5-checked) and dumped with fastq-dump --clip")}


def sample_runs(manifest: list[dict], k: int = RUNS_TO_SAMPLE) -> list[dict]:
    ok = [r for r in manifest if r["status"] == "ok"]
    if len(ok) <= k:
        return ok
    step = len(ok) / k
    return [ok[int(i * step)] for i in range(k)]


def run(pmid: str, label: str) -> dict:
    d = common.protocol_dir(pmid, label)
    protocol = common.protocol_for_tag(pmid, label)
    manifest = common.read_tsv(d / "manifest.tsv")
    catalog = common.load_primer_catalog()
    obs, errors = [], []
    for r in sample_runs(manifest):
        try:
            obs.append(observe_run(r, protocol, catalog))
        except Exception as e:  # noqa: BLE001 - recorded, decision falls to review if all fail
            errors.append(f"{r['run_accession']}: {e}")
    source, sra_note = "ENA FASTQ (first reads, streamed)", None
    if obs and min(o["distinct_quality_values"] for o in obs) <= 2:
        obs, errors, sra_note = quality_fallback(pmid, label, obs, errors, protocol, catalog)
        if sra_note.get("used"):
            source = "NCBI normalized .sra (sampled runs, fetched and dumped)"
    if not obs:
        result = {"decision": "needs_review", "review_reasons": ["could not read any run: " + "; ".join(errors)],
                  "params": {}, "reasons": {}, "observed": {}}
    else:
        result = decide(protocol, obs, catalog)
    if sra_note:
        result["observed"]["quality_fallback"] = sra_note
        if sra_note.get("used"):
            result["observed"]["fastq_source"] = sra_source.SOURCE_NAME
            result["reasons"]["fastq_source"] = sra_note["reason"]
        else:
            result["review_reasons"].append("no full-quality copy at NCBI: " + sra_note["reason"])
    result.update(pmid=pmid, protocol=label, generated_at=dt.datetime.now().isoformat(timespec="seconds"),
                  runs_sampled=[o["run_accession"] for o in obs], read_errors=errors)
    public_obs = [{k: v for k, v in o.items() if not k.startswith("_")} for o in obs]
    common.write_json(d / "preflight.json", {"pmid": pmid, "protocol": label, "reads_per_run": READS_PER_RUN,
                                              "runs": public_obs, "read_errors": errors})
    common.write_json(d / "params.json", result)
    ob = result["observed"]
    detail = (f"protocol {label}: {len(obs)} runs x {READS_PER_RUN} reads; primers_in_reads={ob.get('primers_in_reads')} "
              f"(fwd at R1 start {ob.get('pct_fwd_at_r1_start')}%, rev at R2 start {ob.get('pct_rev_at_r2_start')}%), "
              f"orientation={ob.get('orientation')}; decision={result['decision']}"
              + (f"; review: {' | '.join(result['review_reasons'])}" if result["review_reasons"] else ""))
    common.log_event(pmid, "preflight read check", "success" if result["decision"] == "run" else "partial", detail,
                     source=source, files=[d / "preflight.json", d / "params.json"])
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pmid")
    ap.add_argument("--protocol", default="main")
    a = ap.parse_args()
    res = run(a.pmid, a.protocol)
    print(json.dumps({k: res[k] for k in ("decision", "review_reasons", "params", "observed")}, indent=2, default=str))
