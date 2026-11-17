"""Shared helpers for the Phase 3 pipeline scripts. Standard library only."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STUDIES = REPO / "studies"
PIPELINE = REPO / "pipeline"
ACTOR = "script:pipeline/bin/run_study.py"


# ---------------------------------------------------------------- settings

def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def versions() -> dict[str, str]:
    """Pinned versions from versions.env. NF_PROFILE alone may be overridden from the environment, so the
    same checkout runs with conda on the Mac and with Docker on a Linux VM (NF_PROFILE=docker)."""
    out = _read_env_file(PIPELINE / "versions.env")
    if os.environ.get("NF_PROFILE"):
        out["NF_PROFILE"] = os.environ["NF_PROFILE"]
    return out


def data_dir() -> Path:
    """DATA_DIR from the environment, else from the repo .env, else ~/agentic16s-data."""
    raw = os.environ.get("DATA_DIR") or _read_env_file(REPO / ".env").get("DATA_DIR") or "~/agentic16s-data"
    p = Path(os.path.expanduser(raw)).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def protocol_dir(pmid: str, label: str) -> Path:
    p = data_dir() / "pipeline" / pmid / label
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_protocols(pmid: str) -> list[dict]:
    return json.loads((STUDIES / pmid / "protocol.json").read_text())["protocols"]


def run_tag(label: str, subset: int | None = None) -> str:
    """Folder name for one processing unit: the protocol label, plus _subsetN for smoke runs."""
    return f"{label}_subset{subset}" if subset else label


def protocol_for_tag(pmid: str, tag: str) -> dict:
    label = tag.split("_subset")[0]
    return next(p for p in load_protocols(pmid) if p["label"] == label)


# ---------------------------------------------------------------- logging

def log_event(pmid: str, action: str, outcome: str, detail: str, source: str = "", files=None,
              step: str = "pipeline", actor: str = ACTOR) -> None:
    """Append one line to studies/<PMID>/log.jsonl in the format of studies/README.md."""
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "pmid": pmid,
        "actor": actor,
        "step": step,
        "action": action,
        "source": source,
        "outcome": outcome,
        "detail": detail,
        "files": [str(f) for f in (files or [])],
    }
    with open(STUDIES / pmid / "log.jsonl", "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- tables

def read_tsv(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    fields = fields or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------- sequences

IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T", "U": "T",
    "R": "AG", "Y": "CT", "S": "CG", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}
_COMP = str.maketrans("ACGTURYSWKMBDHVN", "TGCAAYRSWMKVHDBN")


def revcomp(seq: str) -> str:
    """Reverse complement, IUPAC-aware (R<->Y, K<->M, B<->V, D<->H; S, W, N unchanged)."""
    return seq.upper().translate(_COMP)[::-1]


def primer_regex(seq: str) -> re.Pattern:
    """Exact IUPAC match. Read N bases match any primer base."""
    parts = []
    for b in seq.upper():
        alts = IUPAC[b]
        parts.append("[" + alts + "N]")
    return re.compile("".join(parts))


def md5(seq: str) -> str:
    return hashlib.md5(seq.encode()).hexdigest()


def load_primer_catalog() -> list[dict]:
    rows = read_tsv(PIPELINE / "assets" / "primers_16s.tsv")
    for r in rows:
        r["ecoli_start"] = int(r["ecoli_start"])
        r["ecoli_end"] = int(r["ecoli_end"])
    return rows


def catalog_by_seq(seq: str | None) -> dict | None:
    if not seq:
        return None
    for r in load_primer_catalog():
        if r["sequence"].upper() == seq.upper():
            return r
    return None


def expected_amplicon_length(fwd_seq: str | None, rev_seq: str | None) -> int | None:
    """Length between the primers (primers excluded), from E. coli coordinates in the catalog."""
    f, r = catalog_by_seq(fwd_seq), catalog_by_seq(rev_seq)
    if not f or not r:
        return None
    return r["ecoli_start"] - f["ecoli_end"] - 1
