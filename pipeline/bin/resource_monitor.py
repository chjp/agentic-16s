"""Measure what each Nextflow step really uses, for sizing future machines (e.g. a GCP instance).

Nextflow's trace has peak_rss and %cpu on Linux only; on macOS its task wrapper records just the
run time. So while Nextflow runs, a sampler reads `ps` every few seconds:
- each task is a direct child of the Nextflow java process, and its process tree names the task's
  work dir (`.../ab/cdef12.../.command.run`), which is the trace's `hash`;
- per task: peak RSS and peak CPU of the whole tree;
- for the pipeline: peak RSS and CPU of everything together (what the machine must hold).

After the run, `record()` joins the samples with the trace (run times, status) and appends one
entry per Nextflow run to studies/<PMID>/resource_profile.json, failed runs included. Where the
trace itself has peak_rss (Linux), that value wins. Short peaks between samples are missed, so the
numbers are lower bounds; tasks that matter for sizing run for minutes.

  python3 -I pipeline/bin/resource_monitor.py watch <pid> <out.json>   # attach to a running Nextflow
  python3 -I pipeline/bin/resource_monitor.py record <PMID> <tag> <pipeline> [samples.json]
  python3 -I pipeline/bin/resource_monitor.py report                   # table over all studies
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules; python3 -I drops this
import common  # noqa: E402


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

INTERVAL_S = 5
WORKDIR_RE = re.compile(r"(\S+/([0-9a-f]{2})/([0-9a-f]{30}))/\.command\.run")


def snapshot() -> dict[int, tuple[int, int, float, str]]:
    """pid -> (ppid, rss_kb, cpu_pct, command)"""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,pcpu=,command="], capture_output=True, text=True).stdout
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            procs[int(parts[0])] = (int(parts[1]), int(parts[2]), float(parts[3]), parts[4] if len(parts) > 4 else "")
        except ValueError:
            continue
    return procs


def _children(procs: dict) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for pid, (ppid, *_rest) in procs.items():
        kids.setdefault(ppid, []).append(pid)
    return kids


def _tree(pid: int, kids: dict) -> list[int]:
    out, stack = [], [pid]
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(kids.get(p, []))
    return out


def measure(root: int, procs: dict) -> dict | None:
    """One sample: totals under root, and per task (child of the java process)."""
    if root not in procs:
        return None
    kids = _children(procs)
    everything = _tree(root, kids)
    java = next((p for p in everything if "java" in procs[p][3]), root)
    tasks = {}
    for t in kids.get(java, []):
        tree = _tree(t, kids)
        wd = next((m for p in tree if (m := WORKDIR_RE.search(procs[p][3]))), None)
        tasks[t] = {"rss_kb": sum(procs[p][1] for p in tree), "cpu_pct": sum(procs[p][2] for p in tree),
                    "hash": f"{wd.group(2)}/{wd.group(3)[:6]}" if wd else None, "workdir": wd.group(1) if wd else None}
    return {"rss_kb": sum(procs[p][1] for p in everything), "cpu_pct": sum(procs[p][2] for p in everything),
            "tasks": tasks}


class Sampler(threading.Thread):
    """Samples the process tree under `root` until it exits or stop() is called; saves to `out`."""

    def __init__(self, root: int, out: Path, interval: float = INTERVAL_S):
        super().__init__(daemon=True)
        self.root, self.out, self.interval = root, out, interval
        self._stop_event = threading.Event()  # not _stop: Thread.join() calls self._stop() on Python 3.12+
        self.data = {"root_pid": root, "interval_s": interval, "started": now(), "samples": 0,
                     "peak_total_rss_kb": 0, "peak_total_cpu_pct": 0.0, "peak_total_at": None, "tasks": {}}

    def stop(self) -> None:
        self._stop_event.set()

    def add(self, m: dict) -> None:
        d = self.data
        d["samples"] += 1
        if m["rss_kb"] > d["peak_total_rss_kb"]:
            d["peak_total_rss_kb"], d["peak_total_at"] = m["rss_kb"], now()
        d["peak_total_cpu_pct"] = max(d["peak_total_cpu_pct"], m["cpu_pct"])
        for pid, t in m["tasks"].items():
            key = t["hash"] or f"pid:{pid}"
            rec = d["tasks"].setdefault(key, {"native_id": pid, "hash": t["hash"], "workdir": t["workdir"],
                                               "peak_rss_kb": 0, "peak_cpu_pct": 0.0, "samples": 0})
            rec["peak_rss_kb"] = max(rec["peak_rss_kb"], t["rss_kb"])
            rec["peak_cpu_pct"] = max(rec["peak_cpu_pct"], t["cpu_pct"])
            rec["samples"] += 1
            rec["hash"] = rec["hash"] or t["hash"]

    def save(self) -> None:
        self.data["finished"] = now()
        tmp = self.out.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.out)

    def run(self) -> None:
        last_save = 0.0
        while not self._stop_event.is_set():
            try:
                m = measure(self.root, snapshot())
            except Exception:  # noqa: BLE001 - never let the sampler break a pipeline run
                m = None
            if m is None and not self._stop_event.is_set() and self.data["samples"]:
                break  # root exited
            if m:
                self.add(m)
            if time.time() - last_save > 60:
                self.save()
                last_save = time.time()
            self._stop_event.wait(self.interval)
        self.save()


# ---------------------------------------------------------------- trace join

def parse_duration(s: str) -> float | None:
    """Nextflow durations ('46ms', '1.5s', '3m 2s', '1h 2m 3s', '1d 2h') -> seconds."""
    if not s or s == "-":
        return None
    unit = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    total = 0.0
    for num, u in re.findall(r"([\d.]+)\s*(ms|s|m|h|d)", s):
        total += float(num) * unit[u]
    return total


def parse_mem_gb(s: str) -> float | None:
    """Nextflow memory ('512 MB', '1.2 GB', '300 KB') -> GB."""
    m = re.match(r"([\d.]+)\s*([KMGT]?B)", s or "")
    if not m:
        return None
    return float(m.group(1)) * {"B": 1e-9, "KB": 1e-6, "MB": 1e-3, "GB": 1, "TB": 1e3}[m.group(2)]


def short_name(name: str) -> str:
    return name.split(" (")[0].split(":")[-1]


def read_trace(path: Path) -> list[dict]:
    return common.read_tsv(path)


def summarize(trace: list[dict], samples: dict | None) -> dict:
    """Per process: tasks, failures, peak RSS (sampled or from the trace), run times."""
    by_hash = {}
    if samples:
        for t in samples["tasks"].values():
            if t["hash"]:
                by_hash[t["hash"]] = t
    procs: dict[str, dict] = {}
    for row in trace:
        if row.get("status") == "CACHED":
            continue
        p = procs.setdefault(short_name(row["name"]), {"tasks": 0, "failed": 0, "peak_rss_gb": None,
                                                       "peak_cpu_pct": None, "realtime_max_min": 0.0,
                                                       "realtime_sum_h": 0.0, "measured_tasks": 0})
        p["tasks"] += 1
        p["failed"] += row.get("status") not in ("COMPLETED",)
        rt = parse_duration(row.get("realtime", "")) or 0.0
        p["realtime_max_min"] = max(p["realtime_max_min"], rt / 60)
        p["realtime_sum_h"] += rt / 3600
        rss = parse_mem_gb(row.get("peak_rss", ""))
        cpu = None
        s = by_hash.get(row.get("hash", ""))
        if rss is None and s:
            rss = s["peak_rss_kb"] / 1e6 * 1.024  # ps reports KiB
            cpu = s["peak_cpu_pct"]
        elif row.get("%cpu", "-") not in ("-", ""):
            cpu = float(row["%cpu"].rstrip("%"))
        if rss is not None:
            p["measured_tasks"] += 1
            p["peak_rss_gb"] = max(p["peak_rss_gb"] or 0, rss)
        if cpu is not None:
            p["peak_cpu_pct"] = max(p["peak_cpu_pct"] or 0, cpu)
    for p in procs.values():
        for k in ("peak_rss_gb", "realtime_max_min", "realtime_sum_h"):
            if p[k] is not None:
                p[k] = round(p[k], 2)
        if p["peak_cpu_pct"] is not None:
            p["peak_cpu_pct"] = round(p["peak_cpu_pct"])
    order = sorted(procs.items(), key=lambda kv: -(kv[1]["peak_rss_gb"] or 0))
    return {k: v for k, v in order}


def machine() -> dict:
    mem = None
    try:
        mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / (1 << 30)
    except (ValueError, OSError):
        try:
            mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1 << 30)
        except (ValueError, OSError):
            pass
    return {"cpus": os.cpu_count(), "memory_gb": round(mem, 1) if mem else None,
            "platform": sys.platform, "nf_profile": common.versions().get("NF_PROFILE")}


def record(pmid: str, tag: str, pipeline: str, run_dir: Path, samples_path: Path | None,
           scale: dict | None = None, trace_name: str | None = None, note: str | None = None) -> dict | None:
    """Append a Nextflow run to studies/<PMID>/resource_profile.json (keyed by its trace file; the
    latest trace unless trace_name names an earlier one, e.g. to backfill a failed run)."""
    info = run_dir / pipeline / "pipeline_info"
    traces = sorted(info.glob("execution_trace_*.txt")) if info.exists() else []
    if not traces:
        return None
    trace_path = info / trace_name if trace_name else traces[-1]
    latest = trace_path == traces[-1]
    samples = json.loads(samples_path.read_text()) if samples_path and samples_path.exists() else None
    rows = read_trace(trace_path)
    caps = {}
    rc = run_dir / "resources.config"
    if rc.exists():
        m = re.search(r"memory: (\d+)\.GB", rc.read_text())
        c = re.search(r"cpus: (\d+)", rc.read_text())
        caps = {"memory_gb": int(m.group(1)) if m else None, "cpus": int(c.group(1)) if c else None}
    meta = {}
    runjson = run_dir / f"{pipeline}_run.json"
    if latest and runjson.exists():  # <pipeline>_run.json describes the latest run only
        meta = json.loads(runjson.read_text())
    if not meta:
        m = re.search(r"(\d{4}-\d\d-\d\d)_(\d\d)-(\d\d)-(\d\d)", trace_path.name)
        meta = {"started_at": f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}" if m else None,
                "status": "failed" if any(r.get("status") == "FAILED" for r in rows) else None}
    entry = {
        "pipeline": pipeline, "tag": tag, "trace": trace_path.name,
        "started_at": meta.get("started_at"), "finished_at": meta.get("finished_at"),
        "status": meta.get("status"), "machine": machine(), "caps": caps, "scale": scale or {},
        "tasks_run": sum(r.get("status") != "CACHED" for r in rows),
        "tasks_cached": sum(r.get("status") == "CACHED" for r in rows),
        "measurement": ("Nextflow trace peak_rss" if any(r.get("peak_rss", "-") != "-" for r in rows) else
                        f"ps sampling every {samples['interval_s']} s (lower bound)" if samples else
                        "run times only (no memory sampled)"),
        "peak_total_rss_gb": round(samples["peak_total_rss_kb"] / 1e6 * 1.024, 2) if samples else None,
        "peak_total_cpu_pct": round(samples["peak_total_cpu_pct"]) if samples else None,
        "processes": summarize(rows, samples),
    }
    if note:
        entry["note"] = note
    out = common.STUDIES / pmid / "resource_profile.json"
    prof = json.loads(out.read_text()) if out.exists() else {"pmid": pmid, "runs": []}
    prof["runs"] = [r for r in prof["runs"] if r.get("trace") != entry["trace"]] + [entry]
    common.write_json(out, prof)
    return entry


def report() -> str:
    """Markdown table: the heaviest steps of every recorded run, for sizing machines."""
    lines = ["| PMID | tag | status | runs | peak total GB | step | tasks | peak GB | max min | sum h |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for f in sorted(common.STUDIES.glob("*/resource_profile.json")):
        prof = json.loads(f.read_text())
        for r in prof["runs"]:
            top = sorted(r["processes"].items(), key=lambda kv: -((kv[1]["peak_rss_gb"] or 0) * 1000
                                                                   + kv[1]["realtime_max_min"]))[:4]
            for i, (name, p) in enumerate(top):
                head = (f"| {prof['pmid']} | {r['tag']} | {r['status']} | {r['scale'].get('runs', '')} | "
                        f"{r['peak_total_rss_gb'] if r['peak_total_rss_gb'] is not None else '-'} ") if i == 0 \
                    else "| | | | | "
                lines.append(head + f"| {name} | {p['tasks']} | {p['peak_rss_gb'] if p['peak_rss_gb'] is not None else '-'}"
                                    f" | {p['realtime_max_min']} | {p['realtime_sum_h']} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "watch":
        s = Sampler(int(argv[1]), Path(argv[2]))
        s.run()
        print(json.dumps({k: v for k, v in s.data.items() if k != "tasks"}, indent=1))
    elif cmd == "record":
        pmid, tag, pipeline = argv[1:4]
        d = common.protocol_dir(pmid, tag)
        samples = Path(argv[4]) if len(argv) > 4 else d / f"{pipeline}.resources.json"
        n = len(common.read_tsv(d / "samplesheet.tsv")) if (d / "samplesheet.tsv").exists() else None
        e = record(pmid, tag, pipeline, d, samples, {"runs": n})
        print(json.dumps(e, indent=1) if e else "no trace found")
    elif cmd == "report":
        print(report())
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
