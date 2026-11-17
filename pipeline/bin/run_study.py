"""Phase 3 driver: one study from run list to bio.*-shaped tables, then delete the raw reads.

  prepare    run manifest from studies/<PMID>/sample_sheet.tsv + ENA            (make_samplesheet.py)
  preflight  stream a few reads per run, choose ampliseq parameters             (preflight.py)
  fetch      nf-core/fetchngs downloads FASTQ (md5-checked), samplesheet        (make_samplesheet.finalize);
             NCBI's normalized .sra instead when preflight found ENA's quality scores replaced (sra_source.py)
  ampliseq   nf-core/ampliseq: Cutadapt, DADA2, SILVA classification
  collect    tables/ shaped like bio.pipeline_run, run_qc, asv, abundance, taxonomy_assignment
  qc         studies/<PMID>/pipeline_qc.json, verdict pass / review
  load       tables/ into PostgreSQL bio.* and core.run states                (db/load_bio.py; skipped for
             --subset runs and with --no-db)
  cleanup    delete downloaded FASTQ once collect's integrity checks pass and, when loaded, the database
             checks pass too (reads_input = archive spots, abundance sum = non-chimeric reads). Runs that
             failed keep their FASTQ. Skip with --keep-fastq.

Every stage appends to studies/<PMID>/log.jsonl. Large files live under DATA_DIR (see .env).

Examples:
  python3 -I pipeline/bin/run_study.py 35756051 --subset 6          # smoke run, all stages
  python3 -I pipeline/bin/run_study.py 35756051 --from ampliseq     # resume from a stage
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules; python3 -I drops this
import collect  # noqa: E402
import common  # noqa: E402
import make_samplesheet  # noqa: E402
import resource_monitor  # noqa: E402
import preflight  # noqa: E402
import qc_gate  # noqa: E402
import sra_source  # noqa: E402

STAGES = ["prepare", "preflight", "fetch", "ampliseq", "collect", "qc", "load", "cleanup"]
LOCAL_CONFIG = common.PIPELINE / "conf" / "local_mac.config"

# DADA2_DENOISING arguments from nf-core/ampliseq 3.1.0 conf/modules.config, with the homopolymer
# settings DADA2 recommends for 454 and Ion Torrent. Used when 454 reads are trimmed single-end.
DADA2_HOMOPOLYMER_CONFIG = """\
// Written by run_study.py: DADA2 homopolymer settings for 454 reads processed as single-end.
process {
    withName: DADA2_DENOISING {
        ext.args = { [
            'selfConsist = FALSE, priors = character(0), DETECT_SINGLETONS = FALSE, GAPLESS = TRUE, GAP_PENALTY = -8, GREEDY = TRUE, KDIST_CUTOFF = 0.42, MATCH = 5, MAX_CLUST = 0, MAX_CONSIST = 10, MIN_ABUNDANCE = 1, MIN_FOLD = 1, MIN_HAMMING = 1, MISMATCH = -4, OMEGA_A = 1e-40, OMEGA_C = 1e-40, OMEGA_P = 1e-4, PSEUDO_ABUNDANCE = Inf, PSEUDO_PREVALENCE = 2, SSE = 2, USE_KMERS = TRUE, USE_QUALS = TRUE, VECTORIZED_ALIGNMENT = TRUE',
            'BAND_SIZE = 32, HOMOPOLYMER_GAP_PENALTY = -1',
            params.sample_inference == "pseudo" ? "pool = \\"pseudo\\"" :
                params.sample_inference == "pooled" ? "pool = TRUE" : "pool = FALSE"
        ].join(',').replaceAll('(,)*$', "") }
    }
}
"""


class StageError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- environment

def physical_memory() -> int:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def resource_config(d: Path) -> Path:
    """resources.config: caps for each task (resourceLimits) and for all running tasks together
    (executor.cpus/memory), so Nextflow queues work instead of overloading the machine."""
    profile = common.versions()["NF_PROFILE"]
    if "docker" in profile:
        try:
            out = subprocess.run(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"], capture_output=True,
                                 text=True, timeout=60, check=True).stdout.split()
            cpus, mem = int(out[0]), int(out[1])
        except Exception as e:  # noqa: BLE001
            raise StageError(f"Docker is not reachable ({e}); start Docker Desktop, or on Linux add this user to the docker group") from e
        if cpus == 0 or mem == 0:
            raise StageError("Docker daemon is not running; start Docker Desktop, or on Linux the docker service")
        mem_gb, source = max(2, mem // (1 << 30) - 1), "`docker info`"
    else:  # conda/mamba: tasks run natively and share the Mac with macOS and open apps
        cpus = max(2, (os.cpu_count() or 4) - 2)
        mem_gb, source = max(4, physical_memory() // (1 << 30) // 2), "half the physical memory"
    slots = addspecies_slots(mem_gb)
    task_gb = mem_gb // slots
    n = addspecies_chunk(task_gb)
    p = d / "resources.config"
    p.write_text(f"// Written by run_study.py ({source}).\n"
                 f"process {{\n    resourceLimits = [cpus: {cpus}, memory: {mem_gb}.GB, time: 24.h]\n"
                 f"    // addSpecies: single-threaded, so {slots} chunk(s) run side by side, each with its own memory\n"
                 f"    // share and a chunk size n that fits it (see addspecies_slots, addspecies_chunk); ampliseq's\n"
                 f"    // n = 1e5 needs ASVs x 1.47M SILVA species refs x 4 bytes in one matrix\n"
                 f"    withName: DADA2_ADDSPECIES {{\n"
                 f"        cpus = 1\n"
                 f"        // grows on retry (exit 137 = out of memory), up to the whole cap: a fixed value disabled\n"
                 f"        // nf-core's retry-with-more-memory and failed 37661264 three times per chunk (2026-11-16)\n"
                 f"        memory = {{ [{task_gb}.GB * task.attempt, {mem_gb}.GB].min() }}\n"
                 f"        ext.args = {{ ['n = {n}', params.dada_addspecies_allowmultiple ? 'allowMultiple = TRUE' : '',\n"
                 f"                     params.dada_taxonomy_rc ? 'tryRC = TRUE' : 'tryRC = FALSE'].findAll {{ it }}.join(', ') }}\n"
                 f"    }}\n}}\n"
                 f"executor {{\n    cpus = {cpus}\n    memory = '{mem_gb} GB'\n}}\n")
    return p


SPECIES_REFS = 1_466_213  # sequences in SILVA's assignSpecies file (DATA_DIR/refdb/silva_v144_assignSpecies.fa.gz)


def addspecies_slots(mem_gb: int) -> int:
    """How many addSpecies chunks run at once. Each task is single-threaded; asking for the whole memory
    cap made 37661264's 6 chunks run one after another on 1 of 8 vCPUs (2026-11-15). About 10 GB per
    task still gives n >= 130; on the Mac's 8 GB cap this stays 1.
    Measured 2026-11-16 on 37661264 (V3-V4 ASVs of mixed length, 2000 per chunk): 10 GB was not enough
    (killed after ~3 min at n = 130); a 2000-ASV chunk at n = 700 held about 14.5 GB. So 15 GB per task,
    and the memory grows on retry (see resource_config)."""
    return max(1, min(3, mem_gb // 15))


def addspecies_chunk(mem_gb: int) -> int:
    """ASVs per addSpecies chunk, so the task stays within 90 % of the memory cap.

    dada2 matches all ASVs of one length against every species reference at once (integer and
    logical matrices, x2 with tryRC), on top of a fixed cost for the references and their reverse
    complement. Measured on 23013615 (2026-11-15, R 4.2 / dada2 1.30, SILVA 1.47M refs, tryRC):
    n = 50 -> 6.5 GB, n = 100 -> 8.2 GB max R vector memory, about 45 s per chunk, so
    peak ~ refs x (3430 + 23.3 x n) bytes. ampliseq's n = 1e5 put all 2000 ASVs of one file (all
    280 nt after the fixed 454 truncation) in one chunk and hit R's 16 GB limit; n = 200 still did.
    """
    budget = 0.9 * mem_gb * (1 << 30) - SPECIES_REFS * 3430
    n = int(budget // (SPECIES_REFS * 23.3))
    return max(10, n // 10 * 10)


def nextflow(args: list[str], cwd: Path, log_name: str) -> tuple[int, str]:
    """Run Nextflow with the pinned version; stdout goes to <cwd>/<log_name>.stdout.log."""
    env = {**os.environ, "NXF_VER": common.versions()["NXF_VER"],
           "NXF_CONDA_CACHEDIR": str(common.data_dir() / "conda")}
    cmd = ["nextflow", "-log", str(cwd / f"{log_name}.nextflow.log"), "run", *args]
    with open(cwd / f"{log_name}.stdout.log", "w") as fh:
        fh.write(" ".join(cmd) + "\n\n")
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT)
        # per-task peak memory/CPU (Nextflow's trace has none on macOS); see resource_monitor.py
        sampler = resource_monitor.Sampler(proc.pid, cwd / f"{log_name}.resources.json")
        sampler.start()
        proc.wait()
        sampler.stop()
        sampler.join(timeout=30)
    text = (cwd / f"{log_name}.stdout.log").read_text()
    return proc.returncode, text


def record_resources(pmid: str, tag: str, d: Path, pipeline: str) -> None:
    """studies/<PMID>/resource_profile.json: what each step used, failed runs included."""
    try:
        n = len(common.read_tsv(d / "samplesheet.tsv")) if (d / "samplesheet.tsv").exists() else None
        resource_monitor.record(pmid, tag, pipeline, d, d / f"{pipeline}.resources.json", {"runs": n})
    except Exception as e:  # noqa: BLE001 - measuring must never fail a stage
        print(f"resource profile not written: {e}", file=sys.stderr)


def tail(text: str, n: int = 15) -> str:
    return " | ".join(line.strip() for line in text.strip().splitlines()[-n:] if line.strip())


# ---------------------------------------------------------------- stages

def stage_preflight(pmid: str, tag: str, ctx: dict) -> None:
    res = preflight.run(pmid, tag)
    if res["decision"] != "run":
        raise StageError("preflight needs review: " + " | ".join(res["review_reasons"]))


def fetch_from_sra(pmid: str, tag: str, d: Path, outdir: Path) -> None:
    """Runs whose ENA FASTQ lost the quality scores: NCBI's normalized .sra, md5-checked, dumped, .sra deleted."""
    runs = [r["run_accession"] for r in common.read_tsv(d / "manifest.tsv") if r["status"] == "ok"]
    cache = common.STUDIES / pmid / "sources" / "archive"
    started, done, errors = now(), [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(sra_source.fetch, acc, outdir / "fastq", cache): acc for acc in runs}
        for f in as_completed(futs):
            try:
                done.append(f.result())
            except Exception as e:  # noqa: BLE001 - listed in the log; finalize reports the missing runs
                errors.append(f"{futs[f]}: {e}")
    gb = sum(x["sra_bytes"] or 0 for x in done) / 1e9
    common.log_event(pmid, "download FASTQ (NCBI normalized .sra)", "success" if not errors else "partial",
                     f"{tag}: {len(done)} of {len(runs)} runs, {gb:.2f} GB of .sra (md5-checked, dumped with fastq-dump "
                     f"--clip, .sra deleted); started {started}" + (f"; failed: {'; '.join(errors[:5])}" if errors else ""),
                     source="https://sra-pub-run-odp.s3.amazonaws.com (via NCBI SDL locator)",
                     files=[outdir / "fastq"])
    if not done:
        raise StageError("no run could be fetched from NCBI: " + "; ".join(errors[:3]))


# fetchngs' final error names the run whose download failed after its own retries
ENA_FAILED_RUN = re.compile(r"Caused by:\s+Process `[^`]*SRA_FASTQ_FTP \((?:[A-Z]+\d+_)?((?:SRR|ERR|DRR)\d+)\)`")
FALLBACK_FILE = "fastq_fallback.json"


def failed_ena_runs(text: str) -> list[str]:
    """Runs whose ENA FASTQ download failed for good (not the attempts fetchngs retried successfully)."""
    return sorted(set(ENA_FAILED_RUN.findall(text)))


def fallback_limit(n_runs: int) -> int:
    """At most this many runs of a study may come from NCBI instead of ENA. More failures point to a
    network or ENA outage, which should stop the study rather than quietly switch sources."""
    return max(3, n_runs // 20)


def fetch_ena_fallback(pmid: str, tag: str, d: Path, outdir: Path, runs: list[str], reasons: dict[str, str]) -> None:
    """Runs whose ENA FASTQ could not be downloaded: NCBI's normalized .sra, dumped in ENA's format.
    Writes <protocol dir>/fastq_fallback.json, which collect.py copies into pipeline_run.json."""
    cache = common.STUDIES / pmid / "sources" / "archive"
    started, done, errors = now(), {}, []
    for acc in runs:
        try:
            res = sra_source.fetch_split(acc, outdir / "fastq", cache)
            done[acc] = {**res, "source": sra_source.SOURCE_NAME, "reason": reasons.get(acc, ""), "fetched_at": now()}
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(f"{acc}: {e}")
    common.write_json(d / FALLBACK_FILE, done)
    common.log_event(pmid, "download FASTQ (NCBI normalized .sra, ENA fallback)", "success" if not errors else "failure",
                     f"{tag}: {len(done)} of {len(runs)} run(s) whose ENA FASTQ could not be downloaded, taken from NCBI "
                     f"instead ({', '.join(done) or 'none'}; .sra md5-checked, dumped with fastq-dump --split-3 in ENA's "
                     f"read-name format); started {started}" + (f"; failed: {'; '.join(errors)}" if errors else ""),
                     source="https://sra-pub-run-odp.s3.amazonaws.com (via NCBI SDL locator)", files=[d / FALLBACK_FILE])
    if errors:
        raise StageError("NCBI fallback failed: " + "; ".join(errors))


def stage_fetch(pmid: str, tag: str, ctx: dict) -> None:
    d = common.protocol_dir(pmid, tag)
    v = common.versions()
    outdir = common.data_dir() / "fastq" / pmid / tag
    params = json.loads((d / "params.json").read_text())
    (d / FALLBACK_FILE).unlink(missing_ok=True)
    if params.get("observed", {}).get("fastq_source") == sra_source.SOURCE_NAME:
        fetch_from_sra(pmid, tag, d, outdir)
        make_samplesheet.finalize(pmid, tag, outdir)
        return
    work = common.data_dir() / "nf-work" / pmid / tag / "fetch"
    started = now()
    all_runs = [line.strip() for line in (d / "runs.csv").read_text().splitlines() if line.strip()]
    limit = fallback_limit(len(all_runs))
    excluded: dict[str, str] = {}  # run -> why ENA's file was given up on
    while True:
        runs_file = d / "runs.csv"
        if excluded:  # same runs minus the broken ones; -resume reuses every finished download
            runs_file = d / "runs_ena.csv"
            runs_file.write_text("".join(f"{r}\n" for r in all_runs if r not in excluded))
        code, text = nextflow(["nf-core/fetchngs", "-r", v["FETCHNGS_VERSION"], "-profile", v["NF_PROFILE"],
                               "-c", str(LOCAL_CONFIG), "-c", str(resource_config(d)),
                               "--input", str(runs_file), "--outdir", str(outdir),
                               "--download_method", "ftp", "--nf_core_pipeline", "ampliseq",
                               "-work-dir", str(work), "-resume"], d, "fetchngs")
        if code == 0:
            break
        new = [r for r in failed_ena_runs(text) if r not in excluded and r in all_runs]
        ok_to_fallback = new and len(excluded) + len(new) <= limit and sra_source.dump_cmd(sra_source.SPLIT_DUMP)
        common.log_event(pmid, "download FASTQ (nf-core/fetchngs)", "partial" if ok_to_fallback else "failure",
                         f"{tag}: exit {code}: {tail(text)}"
                         + (f"; ENA file(s) of {', '.join(new)} given up on, they will come from NCBI" if ok_to_fallback
                            else f"; no NCBI fallback (failed runs {new or 'not identified'}, limit {limit} per study"
                                 f"{', sra-tools missing' if not sra_source.dump_cmd(sra_source.SPLIT_DUMP) else ''})"),
                         source="ENA FTP", files=[d / "fetchngs.stdout.log"], step="pipeline")
        if not ok_to_fallback:
            raise StageError(f"fetchngs failed (exit {code}); see {d / 'fetchngs.stdout.log'}")
        reason = "ENA FASTQ download failed after fetchngs' retries (" + \
                 ("md5 mismatch" if "did NOT match" in text else "download error") + ")"
        excluded.update({r: reason for r in new})
    if excluded:
        fetch_ena_fallback(pmid, tag, d, outdir, sorted(excluded), excluded)
    n = len(list((outdir / "fastq").glob("*.fastq.gz")))
    gb = sum(f.stat().st_size for f in (outdir / "fastq").glob("*.fastq.gz")) / 1e9
    common.log_event(pmid, "download FASTQ (nf-core/fetchngs)", "success",
                     f"{tag}: {n} FASTQ files, {gb:.2f} GB, md5-checked; started {started}"
                     + (f"; {len(excluded)} run(s) from NCBI instead of ENA: {', '.join(sorted(excluded))}"
                        if excluded else ""),
                     source="ENA FTP", files=[outdir / "samplesheet" / "samplesheet.csv"])
    make_samplesheet.finalize(pmid, tag, outdir)


def stage_ampliseq(pmid: str, tag: str, ctx: dict) -> None:
    d = common.protocol_dir(pmid, tag)
    v = common.versions()
    params = json.loads((d / "params.json").read_text())
    ap = dict(params["params"])
    ap.update(input=str(d / "samplesheet.tsv"), outdir=str(d / "ampliseq"),
              ref_taxonomy_storage=str(common.data_dir() / "refdb"))
    common.write_json(d / "ampliseq_params.json", ap)
    configs = ["-c", str(LOCAL_CONFIG), "-c", str(resource_config(d))]
    if v["NF_PROFILE"] in ("conda", "mamba"):
        configs += ["-c", str(common.PIPELINE / "conf" / "ampliseq_osx_arm64.config")]
    if params["observed"].get("dada2_homopolymer_settings"):
        (d / "extra.config").write_text(DADA2_HOMOPOLYMER_CONFIG)
        configs += ["-c", str(d / "extra.config")]
    work = common.data_dir() / "nf-work" / pmid / tag / "ampliseq"
    started = now()
    code, text = nextflow(["nf-core/ampliseq", "-r", v["AMPLISEQ_VERSION"], "-profile", v["NF_PROFILE"], *configs,
                           "-params-file", str(d / "ampliseq_params.json"), "-work-dir", str(work), "-resume"],
                          d, "ampliseq")
    meta = {"started_at": started, "finished_at": now(), "exit_code": code,
            "status": "succeeded" if code == 0 else "failed",
            "pipeline_commit": pipeline_commit(d),
            "error_message": None if code == 0 else tail(text)}
    common.write_json(d / "ampliseq_run.json", meta)
    record_resources(pmid, tag, d, "ampliseq")
    common.log_event(pmid, "run nf-core/ampliseq", "success" if code == 0 else "failure",
                     f"{tag}: exit {code}, {meta['started_at']} -> {meta['finished_at']}"
                     + ("" if code == 0 else f": {meta['error_message']}"),
                     source=f"nf-core/ampliseq {v['AMPLISEQ_VERSION']}", files=[d / "ampliseq.stdout.log"])
    if code != 0:
        raise StageError(f"ampliseq failed (exit {code}); see {d / 'ampliseq.stdout.log'}")


def pipeline_commit(d: Path) -> str | None:
    """Git commit of the ampliseq checkout: Nextflow keeps each revision in clones/<commit>."""
    for name in ("ampliseq.stdout.log", "ampliseq.nextflow.log"):
        p = d / name
        if p.exists():
            m = re.search(r"ampliseq/clones/([0-9a-f]{40})", p.read_text())
            if m:
                return m.group(1)
    return None


def stage_collect(pmid: str, tag: str, ctx: dict) -> None:
    d = common.protocol_dir(pmid, tag)
    meta = json.loads((d / "ampliseq_run.json").read_text())
    if not meta.get("pipeline_commit"):
        meta["pipeline_commit"] = pipeline_commit(d)
        common.write_json(d / "ampliseq_run.json", meta)
    integrity = collect.collect(pmid, tag, meta)
    if not integrity["pass"]:
        print("integrity problems:", "; ".join(integrity["problems"]), file=sys.stderr)


def stage_qc(pmid: str, tag: str, ctx: dict) -> None:
    out = qc_gate.gate(pmid, tag)
    print(f"QC verdict for {pmid}/{tag}: {out['verdict']}" + (f" ({'; '.join(out['reasons'])})" if out["reasons"] else ""))


def db_tool(args: list[str], d: Path) -> tuple[int, str]:
    """db/load_bio.py needs psycopg, so it runs under uv like the other db/ scripts."""
    cmd = ["uv", "run", "-q", "--with", "psycopg[binary]", "python", str(common.REPO / "db" / "load_bio.py"), str(d), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def use_db(tag: str, ctx: dict) -> bool:
    return not ctx.get("no_db") and "_subset" not in tag


def stage_load(pmid: str, tag: str, ctx: dict) -> None:
    d = common.protocol_dir(pmid, tag)
    if not use_db(tag, ctx):
        why = "--no-db given" if ctx.get("no_db") else "smoke-test subset; only whole studies go into the database"
        common.log_event(pmid, "load bio tables", "skipped", f"{tag}: {why}")
        print(f"load skipped: {why}")
        return
    code, out = db_tool([], d)
    if code != 0:
        common.log_event(pmid, "load bio tables", "failure", f"{tag}: {out[-500:]}", files=[d / "tables"])
        raise StageError(f"database load failed: {out[-500:]}")
    counts = json.loads(out[out.index("{"):])
    common.log_event(pmid, "load bio tables", "success",
                     f"{tag}: pipeline_run_id {counts['pipeline_run_id']}, {counts['run_qc']} run_qc rows, "
                     f"{counts['asv_in_run']} ASVs, {counts['abundance']} abundance rows, {counts['taxonomy']} taxonomy rows; "
                     "is_current", source=str(d / "tables"))
    print(out)


def fastq_files(pmid: str, tag: str) -> list[Path]:
    d = common.protocol_dir(pmid, tag)
    roots = [common.data_dir() / "fastq" / pmid / tag, d / "ampliseq"]
    return sorted(p for r in roots if r.exists() for p in r.rglob("*.f*q.gz"))


def stage_cleanup(pmid: str, tag: str, ctx: dict) -> None:
    """Delete raw FASTQ once processing is complete. Accessions, URLs and md5s stay in manifest.tsv."""
    d = common.protocol_dir(pmid, tag)
    files = fastq_files(pmid, tag)
    if ctx.get("keep_fastq"):
        common.log_event(pmid, "delete raw FASTQ", "skipped", f"{tag}: --keep-fastq given; {len(files)} files kept")
        return
    integ_path = d / "tables" / "integrity.json"
    integrity = json.loads(integ_path.read_text()) if integ_path.exists() else {"pass": False, "problems": ["collect has not run"]}
    if not integrity["pass"]:
        common.log_event(pmid, "delete raw FASTQ", "skipped",
                         f"{tag}: kept {len(files)} files because processing is incomplete: {'; '.join(integrity['problems'])}")
        raise StageError("FASTQ kept: integrity checks failed")
    if use_db(tag, ctx):
        code, out = db_tool(["--check"], d)
        if code != 0:
            common.log_event(pmid, "delete raw FASTQ", "skipped",
                             f"{tag}: kept {len(files)} files because the database checks failed: {out[-500:]}")
            raise StageError("FASTQ kept: database checks failed")
    failed = {r["run_accession"] for r in common.read_tsv(d / "tables" / "run_qc.tsv") if r["status"] == "failed"}
    kept = [f for f in files if any(acc in f.name for acc in failed)]
    files = [f for f in files if f not in kept]
    pr_path = d / "tables" / "pipeline_run.json"
    pr = json.loads(pr_path.read_text())
    if not files:  # a rerun of collect/qc after the reads were already deleted
        print(f"no FASTQ left for {pmid}/{tag} (deleted at {pr.get('raw_fastq_deleted_at') or 'unknown'})")
        return
    freed = sum(f.stat().st_size for f in files)
    for f in files:
        f.unlink()
        md5 = f.with_name(f.name + ".md5")
        if md5.exists():
            md5.unlink()
    # Nextflow's cleanup=true empties work/ after success; remove what is left (directories, failed retries)
    for w in (common.data_dir() / "nf-work" / pmid / tag).glob("*"):
        shutil.rmtree(w, ignore_errors=True)
    pr["raw_fastq_deleted_at"] = now()
    common.write_json(pr_path, pr)
    common.log_event(pmid, "delete raw FASTQ", "success",
                     f"{tag}: deleted {len(files)} FASTQ files ({freed / 1e9:.2f} GB) and Nextflow work dirs after "
                     "integrity checks passed; refetch from manifest.tsv (URL + md5)"
                     + (f"; kept {len(kept)} files of {len(failed)} failed runs for debugging" if kept else ""),
                     files=[d / "manifest.tsv"])
    if use_db(tag, ctx):
        code, out = db_tool(["--mark-deleted"], d)
        print(out)
        if code != 0:
            raise StageError(f"FASTQ deleted but core.run not updated: {out[-300:]}")


STAGE_FN = {"preflight": stage_preflight, "fetch": stage_fetch, "ampliseq": stage_ampliseq,
            "collect": stage_collect, "qc": stage_qc, "load": stage_load, "cleanup": stage_cleanup}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pmid")
    ap.add_argument("--subset", type=int, help="process only N runs, balanced across groups (smoke test)")
    ap.add_argument("--from", dest="from_stage", choices=STAGES, default="prepare")
    ap.add_argument("--to", dest="to_stage", choices=STAGES, default="cleanup")
    ap.add_argument("--keep-fastq", action="store_true", help="do not delete raw FASTQ after processing")
    ap.add_argument("--no-db", action="store_true", help="files only: skip the database load and its checks")
    a = ap.parse_args()
    todo = STAGES[STAGES.index(a.from_stage): STAGES.index(a.to_stage) + 1]
    ctx = {"keep_fastq": a.keep_fastq, "no_db": a.no_db}
    if "prepare" in todo:
        tags = list(make_samplesheet.prepare(a.pmid, a.subset))
        todo.remove("prepare")
    else:
        tags = [common.run_tag(p["label"], a.subset) for p in common.load_protocols(a.pmid)]
    rc = 0
    for tag in tags:
        for stage in todo:
            print(f"[{now()}] {a.pmid}/{tag}: {stage}", flush=True)
            try:
                STAGE_FN[stage](a.pmid, tag, ctx)
            except StageError as e:
                print(f"stopped at {stage}: {e}", file=sys.stderr)
                rc = 2
                break
    return rc


if __name__ == "__main__":
    sys.exit(main())
