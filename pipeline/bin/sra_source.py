"""Second FASTQ source: NCBI's full ("normalized") SRA file, for runs whose ENA FASTQ lost the quality scores.

ENA serves FASTQ derived from SRA Lite for some old runs, where every quality is replaced by a
constant (Phred 30 for reads that passed, 3 otherwise). NCBI still keeps the normalized .sra with
the original scores, publicly on the AWS Open Data bucket (no account, no charge). NCBI's locator
API lists every copy of a run with its md5, and flags the Lite copy with "noqual".

  locate(run)              -> {"url", "md5", "size", "name"} of the normalized .sra, or None
  fetch(run, outdir)       -> outdir/<run>.fastq.gz (md5-checked .sra, dumped with sratoolkit, .sra deleted)
  fetch_split(run, outdir) -> outdir/<run>_1.fastq.gz (+ _2) in ENA's own FASTQ format

Second use, fetch_split: runs whose ENA FASTQ file cannot be downloaded at all (ENA lists it with a
size and md5 but serves an HTML page; SRR15400852 read 2, 2026-11-15). The dump uses ENA's read
names ("@<run>.<spot> <spot>/<read>"). Checked on SRR15400852: read 1 dumped from NCBI is identical
to ENA's read 1 after decompression (43838 reads), and both files have exactly ENA's listed byte sizes.

fetch needs sra-tools: fastq-dump on PATH, or the project env made once with
  mamba create -p $DATA_DIR/conda/sra-tools --override-channels -c conda-forge -c bioconda sra-tools
locate needs only the network.

Checked on SRR578443 (2026-11-15): the .sra md5 matches NCBI's, it has 35 distinct quality values
(ENA's FASTQ has 2), and `fastq-dump --clip` gives sequences identical to ENA's for all 4950 reads.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules; python3 -I drops this
import common  # noqa: E402

SDL = "https://locate.ncbi.nlm.nih.gov/sdl/2/retrieve?acc={acc}&accept-alternate-locations=yes"
SOURCE_NAME = "ncbi_sra_normalized"
# fastq-dump rather than fasterq-dump: it reads a local .sra and applies the 454 clip points (--clip),
# so key, barcode and adapter tails are cut the way the archive's own FASTQ is.
DUMP = ["fastq-dump", "--clip", "--stdout"]
# Illumina runs replacing an ENA file: biological reads split into _1/_2, with ENA's read names
SPLIT_DUMP = ["fastq-dump", "--split-3", "--skip-technical", "--defline-seq", "@$ac.$si $si/$ri",
              "--defline-qual", "+", "--gzip"]


def _exe(name: str) -> str | None:
    exe = shutil.which(name)
    if not exe:
        cand = common.data_dir() / "conda" / "sra-tools" / "bin" / name
        exe = str(cand) if cand.exists() else None
    return exe


def dump_cmd(args: list[str] = DUMP) -> list[str] | None:
    exe = _exe(args[0])
    return [exe, *args[1:]] if exe else None


def _get(url: str, attempts: int = 3) -> bytes:
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))
    raise AssertionError


def pick_normalized(sdl: dict) -> dict | None:
    """The .sra copy that keeps quality scores and can be fetched without payment."""
    for res in sdl.get("result", []):
        for f in res.get("files", []):
            if f.get("type") != "sra" or f.get("noqual") or f.get("name", "").endswith(".lite"):
                continue
            for loc in f.get("locations", []):
                if loc.get("link") and not loc.get("payRequired") and not loc.get("rehydrationRequired"):
                    return {"url": loc["link"], "md5": f.get("md5"), "size": f.get("size"), "name": f.get("name"),
                            "service": loc.get("service")}
    return None


def locate(run: str, cache_dir: Path | None = None) -> dict | None:
    """Query NCBI's locator; the raw response is cached (raw layer) when cache_dir is given."""
    cache = cache_dir / f"ncbi_sdl_{run}.json" if cache_dir else None
    if cache and cache.exists():
        sdl = json.loads(cache.read_text())
    else:
        sdl = json.loads(_get(SDL.format(acc=run)))
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(sdl, indent=1))
    return pick_normalized(sdl)


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def have_tools() -> bool:
    return dump_cmd() is not None


def download_sra(run: str, loc: dict, sra: Path) -> None:
    """Download the .sra to `sra`, up to 3 times until its md5 matches NCBI's."""
    for i in range(3):
        sra.write_bytes(_get(loc["url"]))
        if not loc["md5"] or md5_file(sra) == loc["md5"]:
            return
        if i == 2:
            sra.unlink()
            raise RuntimeError(f"{run}: md5 mismatch after 3 downloads")


def fetch_split(run: str, outdir: Path, cache_dir: Path | None = None) -> dict:
    """NCBI's normalized .sra (md5-checked) dumped to <run>_1.fastq.gz and <run>_2.fastq.gz (one file for
    single-end runs) in ENA's FASTQ format; the .sra is deleted."""
    if not dump_cmd(SPLIT_DUMP):
        raise RuntimeError("sra-tools not found (see the module docstring for the conda env)")
    loc = locate(run, cache_dir)
    if not loc:
        raise RuntimeError(f"{run}: NCBI has no normalized .sra with quality scores")
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = outdir / f".{run}.ncbi"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    sra = tmp / f"{run}.sra"  # the file stem becomes $ac in the read names
    try:
        download_sra(run, loc, sra)
        size = sra.stat().st_size
        proc = subprocess.run([*dump_cmd(SPLIT_DUMP), "-O", str(tmp), str(sra)], capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"{run}: {SPLIT_DUMP[0]} failed: {proc.stderr.decode()[-300:]}")
        files = sorted(tmp.glob(f"{run}*.fastq.gz"))
        if not files:
            raise RuntimeError(f"{run}: {SPLIT_DUMP[0]} wrote no FASTQ")
        paths = []
        for f in files:
            dest = outdir / f.name
            f.replace(dest)
            paths.append(str(dest))
        with gzip.open(paths[0], "rt") as fh:
            reads = sum(1 for _ in fh) // 4
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"run": run, "paths": paths, "reads": reads, "sra_md5": loc["md5"], "sra_bytes": size, "url": loc["url"]}


def fetch(run: str, outdir: Path, cache_dir: Path | None = None) -> dict:
    """Download the normalized .sra, check md5, dump to <run>.fastq.gz, delete the .sra."""
    if not have_tools():
        raise RuntimeError("sra-tools not found (see the module docstring for the conda env)")
    loc = locate(run, cache_dir)
    if not loc:
        raise RuntimeError(f"{run}: NCBI has no normalized .sra with quality scores")
    outdir.mkdir(parents=True, exist_ok=True)
    sra = outdir / f"{run}.sra"
    out = outdir / f"{run}.fastq.gz"
    if out.exists():  # fetched earlier (preflight samples, or a resumed fetch)
        with gzip.open(out, "rt") as fh:
            reads = sum(1 for _ in fh) // 4
        return {"run": run, "path": str(out), "reads": reads, "sra_md5": loc["md5"], "sra_bytes": loc["size"],
                "url": loc["url"], "reused": True}
    download_sra(run, loc, sra)
    proc = subprocess.run([*dump_cmd(), str(sra)], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{run}: {DUMP[0]} failed: {proc.stderr.decode()[-300:]}")
    # fastq-dump names reads after the file; restore the run accession
    text = proc.stdout.decode().replace(f"@{sra.stem}.", f"@{run}.").replace(f"+{sra.stem}.", f"+{run}.")
    tmp = out.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as fh:
        fh.write(text)
    tmp.rename(out)
    reads = text.count("\n") // 4
    size = sra.stat().st_size
    sra.unlink()
    return {"run": run, "path": str(out), "reads": reads, "sra_md5": loc["md5"], "sra_bytes": size, "url": loc["url"]}


if __name__ == "__main__":
    print(json.dumps(locate(sys.argv[1]), indent=1))
