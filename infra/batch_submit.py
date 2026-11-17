#!/usr/bin/env python3
"""Run one study as a Google Batch job, from the Mac (one job = one study = one VM).

  python3 -I infra/batch_submit.py --probe                  # what Batch's Debian image has; e2-standard-2, ~10 min
  python3 -I infra/batch_submit.py 35756051 --subset 6 --compare ~/agentic16s-data/pipeline/35756051/main_subset6
  python3 -I infra/batch_submit.py 35756051 --compare ~/agentic16s-data/pipeline/35756051/main
  python3 -I infra/batch_submit.py <PMID>                   # new study: prepare here, run on Batch, load here
  python3 -I infra/batch_submit.py --collect <run_id>       # fetch and merge results of a job submitted earlier

What happens (the GCE worker's steps, minus the VM handling; see docs/gcp-migration.md section 8):
  1. checks: the gcloud token belongs to the configured account (two Google accounts on this Mac), the
     old worker VM is off (it would take 8 of the project's 12 vCPUs)
  2. inputs: manifest.tsv + runs.csv per tag, from `run_study.py --to prepare` (needs PostgreSQL), or with
     --compare from the reference run, so both runs see exactly the same runs
  3. a fixed code snapshot (pipeline/ + studies/<PMID>/) goes to gs://<bucket>/code/<sha>.tgz, so a fix
     made later cannot leak into a running job (experience log E13)
  4. the job runs infra/batch_runner.sh on a standard (non-Spot) VM and uploads tables, reports, logs
  5. the Mac downloads them, merges studies/<PMID>/ (new log lines, new resource-profile runs, QC file),
     and then either compares with the reference (--compare) or loads the database (run_study --from load)
Every job is recorded in studies/<PMID>/batch_runs.jsonl (state, minutes, estimated cost).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pipeline" / "bin"))
import common  # noqa: E402
import compare_tables  # noqa: E402

GCLOUD = str(REPO / "infra" / "gcloud16s")
TERRAFORM = shutil.which("terraform") or str(Path.home() / ".local" / "bin" / "terraform")
RUNNER = REPO / "infra" / "batch_runner.sh"
# Credit-eligible general-purpose types only (Spot and c2/c3/m-series are excluded from credits).
# vCPU, GB, approximate on-demand $/h in us-central1 (docs/gcp-migration.md section 4; estimates).
MACHINES = {"e2-standard-2": (2, 8, 0.067), "e2-standard-4": (4, 16, 0.134),
            "e2-standard-8": (8, 32, 0.268), "n2d-highmem-8": (8, 64, 0.45)}
QUOTA_VCPUS = 12  # CPUS_ALL_REGIONS on the free-trial project (measured 2026-11-15)
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
SNAPSHOT_SKIP = {"__pycache__", ".pytest_cache", "work"}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def run(cmd: list[str], check: bool = True) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr.strip()[-2000:]}")
    return p.stdout


def gcloud(*args: str, check: bool = True) -> str:
    return run([GCLOUD, *args], check=check)


# ---------------------------------------------------------------- pure helpers (unit-tested)

def job_config(*, runner_text: str, env: dict, machine: str, region: str, network: str, subnetwork: str,
               service_account: str, max_min: int, disk_gb: int, labels: dict) -> dict:
    """Batch job: one task on one standard VM of Batch's Debian image, logs to Cloud Logging."""
    if machine not in MACHINES:
        raise ValueError(f"machine {machine} not in {sorted(MACHINES)}")
    return {
        "taskGroups": [{
            "taskCount": 1,
            "taskSpec": {
                "runnables": [{"script": {"text": runner_text}}],
                "environment": {"variables": env},
                # the runner stops run_study.py at max_min and still needs time to upload
                "maxRunDuration": f"{(max_min + 40) * 60}s",
                "maxRetryCount": 0,
            },
        }],
        "allocationPolicy": {
            "location": {"allowedLocations": [f"regions/{region}"]},
            "instances": [{"policy": {
                "machineType": machine,
                "provisioningModel": "STANDARD",
                "bootDisk": {"image": "batch-debian", "type": "pd-balanced", "sizeGb": disk_gb},
            }}],
            "network": {"networkInterfaces": [{"network": network, "subnetwork": subnetwork,
                                               "noExternalIpAddress": False}]},
            "serviceAccount": {"email": service_account},
            "labels": {"app": "agentic16s"},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
        "labels": labels,
    }


def code_snapshot(repo: Path, pmid: str | None) -> bytes:
    """Deterministic .tgz of pipeline/, the two Batch scripts (their unit tests run on the VM in a probe)
    and studies/<PMID>/ (same content -> same bytes -> same sha)."""
    roots = [repo / "pipeline", repo / "infra" / "batch_submit.py", repo / "infra" / "batch_runner.sh"] + \
        ([repo / "studies" / pmid] if pmid else [])
    files = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and not (SNAPSHOT_SKIP & set(p.relative_to(repo).parts)):
                files.append(p)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for p in files:
            info = tarfile.TarInfo(str(p.relative_to(repo)))
            data = p.read_bytes()
            info.size, info.mtime, info.mode = len(data), 0, 0o755 if os.access(p, os.X_OK) else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def inputs_archive(pmid: str, tag_dirs: dict[str, Path]) -> bytes:
    """manifest.tsv + runs.csv per tag, laid out as DATA_DIR/pipeline/<PMID>/<tag>/ on the VM."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for tag, d in sorted(tag_dirs.items()):
            for name in ("manifest.tsv", "runs.csv"):
                src = d / name
                if not src.exists():
                    raise SystemExit(f"{src} is missing; run prepare first (or pick another --compare dir)")
                data = src.read_bytes()
                info = tarfile.TarInfo(f"pipeline/{pmid}/{tag}/{name}")
                info.size, info.mtime, info.mode = len(data), 0, 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def new_log_lines(snapshot_lines: int, vm_text: str, host: str) -> list[str]:
    """Lines the job appended to studies/<PMID>/log.jsonl, labelled with where they ran."""
    out = []
    for line in vm_text.splitlines()[snapshot_lines:]:
        if line.strip():
            rec = json.loads(line)
            rec.setdefault("host", host)
            out.append(json.dumps(rec, ensure_ascii=False))
    return out


def profile_key(r: dict) -> tuple:
    return (r.get("trace"), r.get("started_at"))


def merge_profile(local: dict | None, vm: dict, snapshot_keys: set, host: str) -> dict:
    """Append the runs the job added to resource_profile.json; keep everything already on the Mac."""
    merged = local or {"pmid": vm.get("pmid"), "runs": []}
    have = {profile_key(r) for r in merged["runs"]}
    for r in vm.get("runs", []):
        k = profile_key(r)
        if k not in snapshot_keys and k not in have:
            merged["runs"].append({**r, "host": host})
    return merged


def status_times(job: dict) -> dict:
    """First RUNNING and the terminal event from the job's status events, plus Batch's run duration."""
    st = job.get("status", {})
    out = {"state": st.get("state"), "run_duration_s": None, "running_at": None, "finished_at": None}
    rd = st.get("runDuration")
    if rd:
        out["run_duration_s"] = float(str(rd).rstrip("s"))
    for ev in st.get("statusEvents", []):
        desc = ev.get("description", "")
        if " to RUNNING" in desc and not out["running_at"]:
            out["running_at"] = ev.get("eventTime")
        if any(f" to {s}" in desc for s in TERMINAL):
            out["finished_at"] = ev.get("eventTime")
    return out


# ---------------------------------------------------------------- checks

def check_identity() -> str:
    """The gcloud token must belong to the configured account (Antigravity once swapped it; CLAUDE.md)."""
    account = gcloud("config", "get-value", "account").strip()
    token = gcloud("auth", "print-access-token").strip()
    data = urllib.parse.urlencode({"access_token": token}).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/tokeninfo", data=data, timeout=30) as r:
        email = json.load(r).get("email")
    if email != account:
        raise SystemExit(f"gcloud token belongs to {email}, configured account is {account}: "
                         "see 'Two Google accounts' in ~/.claude/CLAUDE.md before going on")
    return email


def busy_vcpus(region: str) -> int:
    """vCPUs taken by the old worker VM and by active agentic16s Batch jobs."""
    n = 0
    status = gcloud("compute", "instances", "list", "--filter=name=agentic16s-worker",
                    "--format=value(status)", check=False).strip()
    if status == "RUNNING":
        print("the old GCE worker is RUNNING (8 vCPUs); stop it first: "
              "terraform -chdir=infra apply -var worker_running=false")
        n += 8
    jobs = json.loads(gcloud("batch", "jobs", "list", "--location", region, "--format=json", check=False) or "[]")
    for j in jobs:
        if j.get("status", {}).get("state") in ("QUEUED", "SCHEDULED", "RUNNING") and \
                j.get("labels", {}).get("app") == "agentic16s":
            m = j["allocationPolicy"]["instances"][0]["policy"]["machineType"]
            n += MACHINES.get(m, (8,))[0]
            print(f"active job {j['name'].rsplit('/', 1)[-1]} ({m}, {j['status']['state']})")
    return n


def print_estimate(pmid: str, tag_dirs: dict[str, Path], machine: str) -> None:
    """Read counts of this study next to the measured GCE runs (experience log E11): say it up front."""
    measured = {"29335008": "36 min (pooled)", "35756051": "1 h 13 min (pseudo, incl. 9 min addSpecies)",
                "28174747": "denoising alone 76 min (pseudo, 1x101)"}

    def reads(d: Path) -> tuple[int, int]:
        rows = common.read_tsv(d / "manifest.tsv")
        return len(rows), sum(int(r.get("read_count") or 0) for r in rows)

    for tag, d in tag_dirs.items():
        n, total = reads(d)
        print(f"{pmid}/{tag}: {n} runs, {total / 1e6:.1f} M reads, on {machine}")
    for ref, text in measured.items():
        m = common.data_dir() / "pipeline" / ref / "main" / "manifest.tsv"
        if m.exists():
            n, total = reads(m.parent)
            print(f"  measured on e2-standard-8: {ref}: {n} runs, {total / 1e6:.1f} M reads, ampliseq {text}")


# ---------------------------------------------------------------- submit, wait, collect

def stage_dir(run_id: str) -> Path:
    return common.data_dir() / "batch" / "runs" / run_id


def submit(a, tf: dict) -> str:
    region = tf["batch_region"]
    pmid = a.pmid
    if not a.probe:
        if not (REPO / "studies" / pmid).is_dir():
            raise SystemExit(f"studies/{pmid} does not exist")
        tags = [common.run_tag(p["label"], a.subset) for p in common.load_protocols(pmid)]
        if a.compare:
            if len(tags) != 1:
                raise SystemExit(f"--compare takes one protocol directory; {pmid} has tags {tags}")
            tag_dirs = {tags[0]: a.compare}
        else:
            prep = [sys.executable, "-I", str(REPO / "pipeline" / "bin" / "run_study.py"), pmid, "--to", "prepare"]
            subprocess.run(prep + (["--subset", str(a.subset)] if a.subset else []), check=True)
            tag_dirs = {t: common.protocol_dir(pmid, t) for t in tags}
        print_estimate(pmid, tag_dirs, a.machine)

    need = MACHINES[a.machine][0]
    busy = busy_vcpus(region)
    if busy + need > QUOTA_VCPUS:
        raise SystemExit(f"{busy} vCPUs busy + {need} for this job > quota {QUOTA_VCPUS}; wait or pick a smaller machine")

    now = utcnow()
    run_id = f"a16s-{pmid or 'probe'}-{now:%Y%m%d-%H%M%S}"
    sd = stage_dir(run_id)
    sd.mkdir(parents=True)
    bucket = tf["batch_bucket"]
    snap = code_snapshot(REPO, pmid)
    sha = hashlib.sha256(snap).hexdigest()[:16]
    (sd / "code.tgz").write_bytes(snap)
    code_uri = f"gs://{bucket}/code/{sha}.tgz"
    if not gcloud("storage", "ls", code_uri, check=False).strip():
        gcloud("storage", "cp", "--quiet", str(sd / "code.tgz"), code_uri)
    env = {"MODE": "probe" if a.probe else "run", "CODE_URI": code_uri,
           "OUT_URI": f"gs://{bucket}/runs/{run_id}/output", "PMID": pmid or "",
           "RUN_ARGS": f"--subset {a.subset}" if a.subset else "", "MAX_MIN": str(a.max_min)}
    state = {"run_id": run_id, "job": run_id, "region": region, "mode": env["MODE"], "pmid": pmid,
             "subset": a.subset, "machine": a.machine, "code_sha": sha, "submitted_at": now.isoformat(timespec="seconds"),
             "compare": str(a.compare) if a.compare else None, "out_uri": env["OUT_URI"]}
    if not a.probe:
        (sd / "inputs.tgz").write_bytes(inputs_archive(pmid, tag_dirs))
        env["INPUT_URI"] = f"gs://{bucket}/runs/{run_id}/input/inputs.tgz"
        gcloud("storage", "cp", "--quiet", str(sd / "inputs.tgz"), env["INPUT_URI"])
        sdir = REPO / "studies" / pmid
        log = sdir / "log.jsonl"
        prof = sdir / "resource_profile.json"
        state["tags"] = sorted(tag_dirs)
        state["snapshot_log_lines"] = len(log.read_text().splitlines()) if log.exists() else 0
        state["snapshot_profile_keys"] = [list(profile_key(r)) for r in
                                          (json.loads(prof.read_text())["runs"] if prof.exists() else [])]
    cfg = job_config(runner_text=RUNNER.read_text(), env=env, machine=a.machine, region=region,
                     network=tf["batch_network"], subnetwork=tf["batch_subnetwork"],
                     service_account=tf["batch_service_account"], max_min=a.max_min, disk_gb=a.disk_gb,
                     labels={"app": "agentic16s", "pmid": pmid or "probe", "mode": env["MODE"]})
    (sd / "job.json").write_text(json.dumps(cfg, indent=2))
    common.write_json(sd / "submit.json", state)
    gcloud("batch", "jobs", "submit", run_id, "--location", region, "--config", str(sd / "job.json"), "--format=json")
    print(f"submitted {run_id} ({a.machine}, code {sha}); staging {sd}")
    if pmid:
        common.log_event(pmid, "run study on Google Batch", "started",
                         f"{run_id}: {a.machine}, code snapshot {sha}, tags {state.get('tags')}"
                         + (f", compare with {a.compare}" if a.compare else ""))
    return run_id


def wait(run_id: str, region: str, poll_s: int = 60) -> dict:
    last, errors = None, 0
    while True:
        try:
            job = json.loads(gcloud("batch", "jobs", "describe", run_id, "--location", region, "--format=json"))
            errors = 0
        except SystemExit as e:  # transient network or API error: keep waiting, give up after ~15 min
            errors += 1
            if errors > 15:
                raise
            print(f"describe failed ({errors}): {str(e)[:200]}")
            time.sleep(poll_s)
            continue
        state = job.get("status", {}).get("state")
        if state != last:
            print(f"[{utcnow():%H:%M:%S} UTC] {run_id}: {state}", flush=True)
            last = state
        if state in TERMINAL:
            return job
        time.sleep(poll_s)


def collect(run_id: str) -> int:
    sd = stage_dir(run_id)
    st = json.loads((sd / "submit.json").read_text())
    job = json.loads(gcloud("batch", "jobs", "describe", run_id, "--location", st["region"], "--format=json"))
    times = status_times(job)
    out = sd / "output"
    out.mkdir(exist_ok=True)
    p = subprocess.run([GCLOUD, "storage", "cp", "--quiet", f"{st['out_uri']}/*", str(out)], capture_output=True, text=True)
    if p.returncode != 0:  # e.g. a copy skipped for want of a fast CRC32C (see infra/gcloud16s)
        print(f"warning: download from {st['out_uri']} incomplete (exit {p.returncode}):\n{p.stderr.strip()[-800:]}")
    if not (out / "runner.log").exists():  # the VM died before uploading: take the log from Cloud Logging
        flt = f'logName="projects/{job["name"].split("/")[1]}/logs/batch_task_logs" AND labels.job_uid="{job["uid"]}"'
        text = gcloud("logging", "read", flt, "--order=asc", "--limit=5000", "--format=value(textPayload)", check=False)
        (out / "runner.cloudlogging.log").write_text(text)
        print(f"no files uploaded; Cloud Logging copy in {out / 'runner.cloudlogging.log'}")
    secs = times["run_duration_s"] or 0
    price = MACHINES[st["machine"]][2]
    record = {**{k: st[k] for k in ("run_id", "mode", "pmid", "subset", "machine", "code_sha", "submitted_at")},
              **times, "run_min": round(secs / 60, 1),
              "est_cost_usd": round((secs + 120) / 3600 * price, 3),  # +2 min VM boot before RUNNING
              "exit_code": (out / "exit_code").read_text().strip() if (out / "exit_code").exists() else None}
    print(json.dumps(record, indent=2))
    if st["mode"] == "probe":
        if (out / "probe.txt").exists():
            print((out / "probe.txt").read_text())
        common.write_json(sd / "record.json", record)
        return 0 if times["state"] == "SUCCEEDED" else 1

    pmid = st["pmid"]
    host = f"batch:{run_id} ({st['machine']}, NF_PROFILE=docker)"
    res = out / "results.tgz"
    rc = 0 if times["state"] == "SUCCEEDED" else 1
    if res.exists():
        ex = sd / "extracted"
        shutil.rmtree(ex, ignore_errors=True)
        with tarfile.open(res) as tar:
            tar.extractall(ex)  # our own archive, written by batch_runner.sh
        before = snapshot_files((sd / "code.tgz").read_bytes(), f"studies/{pmid}/")
        merge_studies(st, ex / "repo" / "studies" / pmid, REPO / "studies" / pmid, host, before)
        for tag in st["tags"]:
            src = ex / "data" / "pipeline" / pmid / tag
            if not src.exists():
                continue
            dest = (common.data_dir() / "batch" / pmid / tag) if st["compare"] else common.protocol_dir(pmid, tag)
            shutil.copytree(src, dest, dirs_exist_ok=True)
            record.setdefault("results", {})[tag] = str(dest)
            if st["compare"] and not (dest / "tables" / "run_qc.tsv").exists():
                print(f"compare {tag}: skipped, the job produced no tables")
            elif st["compare"]:
                cmp = compare_tables.compare(Path(st["compare"]), dest)
                name = "batch_compare.json" if "_subset" not in tag else f"batch_compare_{tag.split('_', 1)[1]}.json"
                common.write_json(REPO / "studies" / pmid / name, {"run_id": run_id, **cmp})
                record.setdefault("compare", {})[tag] = {k: cmp[k] for k in ("counts_identical",)} | \
                    {"params_differ": sorted(cmp["params_differ"]), "taxonomy": cmp["taxonomy"]["label_differs"]}
                print(f"compare {tag}: counts identical = {cmp['counts_identical']}; params differ: "
                      f"{sorted(cmp['params_differ']) or 'none'}; taxonomy label differences {cmp['taxonomy']['label_differs']}")
    else:
        print(f"no results.tgz from the job; logs in {out}")
        rc = 1
    # A second --collect of the same job replaces its record and logs again only if the outcome changed.
    runs_file = REPO / "studies" / pmid / "batch_runs.jsonl"
    old = [json.loads(x) for x in runs_file.read_text().splitlines() if x.strip()] if runs_file.exists() else []
    prev = next((r for r in old if r.get("run_id") == run_id), None)
    if prev is None or (prev.get("state"), "results" in prev) != (record.get("state"), "results" in record):
        common.log_event(pmid, "run study on Google Batch", "success" if rc == 0 else "failure",
                         f"{run_id}: {times['state']}, exit {record['exit_code']}, {record['run_min']} min on {st['machine']}, "
                         f"~${record['est_cost_usd']} (estimate); staging {sd}")
    runs_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                 for r in [r for r in old if r.get("run_id") != run_id] + [record]))
    common.write_json(sd / "record.json", record)
    if rc == 0 and not st["compare"] and not st["subset"]:
        print("loading into PostgreSQL on the Mac: run_study.py --from load")
        rc = subprocess.run([sys.executable, "-I", str(REPO / "pipeline" / "bin" / "run_study.py"), pmid,
                             "--from", "load"]).returncode
    return rc


def snapshot_files(snapshot: bytes, prefix: str) -> dict[str, bytes]:
    """Files under `prefix` in a code snapshot, keyed by path relative to it."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(snapshot), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile() and m.name.startswith(prefix):
                out[m.name[len(prefix):]] = tar.extractfile(m).read()
    return out


def merge_studies(st: dict, vm_dir: Path, local_dir: Path, host: str, before: dict[str, bytes]) -> None:
    """Bring the job's changes to studies/<PMID>/ back: appended log lines, added profile runs, QC files
    (suffixed _batch for comparison runs), new cache files. `before` is the snapshot the job started
    from, so files the job did not touch are left alone. Anything else it changed is reported."""
    if not vm_dir.exists():
        return
    vm_log = vm_dir / "log.jsonl"
    if vm_log.exists():
        local_log = local_dir / "log.jsonl"
        have = set(local_log.read_text().splitlines()) if local_log.exists() else set()
        lines = [x for x in new_log_lines(st["snapshot_log_lines"], vm_log.read_text(), host) if x not in have]
        with open(local_log, "a") as fh:  # a second --collect of the same job adds nothing
            fh.writelines(line + "\n" for line in lines)
        print(f"log.jsonl: {len(lines)} new lines from the job")
    vm_prof = vm_dir / "resource_profile.json"
    if vm_prof.exists():
        lp = local_dir / "resource_profile.json"
        merged = merge_profile(json.loads(lp.read_text()) if lp.exists() else None, json.loads(vm_prof.read_text()),
                               {tuple(k) for k in st["snapshot_profile_keys"]}, host)
        common.write_json(lp, merged)
    for f in sorted(vm_dir.rglob("*")):
        rel = f.relative_to(vm_dir)
        if not f.is_file() or rel.name in ("log.jsonl", "resource_profile.json"):
            continue
        if before.get(rel.as_posix()) == f.read_bytes():
            continue  # unchanged by the job
        dest = local_dir / rel
        if rel.name.startswith("pipeline_qc"):
            if st["compare"]:
                dest = dest.with_name(dest.stem + "_batch.json")
            shutil.copy2(f, dest)
            print(f"{dest.relative_to(REPO)} written")
        elif not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        elif dest.read_bytes() != f.read_bytes():
            print(f"note: the job changed {rel}; the Mac copy is kept, the job's copy is in {f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pmid", nargs="?")
    ap.add_argument("--probe", action="store_true", help="environment check only (no study)")
    ap.add_argument("--subset", type=int, help="smoke test: N runs (same as run_study.py --subset)")
    ap.add_argument("--compare", type=Path, help="reference protocol dir: take its manifest/runs.csv, compare tables, no load")
    ap.add_argument("--machine", choices=sorted(MACHINES), help="default e2-standard-8 (probe: e2-standard-2)")
    ap.add_argument("--max-min", type=int, help="stop run_study.py after this many minutes (default 360; probe 30)")
    ap.add_argument("--disk-gb", type=int, default=50, help="boot disk; the GCE worker had 50 GB")
    ap.add_argument("--no-wait", action="store_true", help="submit and return; collect later with --collect")
    ap.add_argument("--collect", metavar="RUN_ID", help="download and merge the results of a finished job")
    a = ap.parse_args()
    if a.collect:
        return collect(a.collect)
    if bool(a.probe) == bool(a.pmid):
        ap.error("give a PMID or --probe")
    a.machine = a.machine or ("e2-standard-2" if a.probe else "e2-standard-8")
    a.max_min = a.max_min or (30 if a.probe else 360)
    if a.compare:
        a.compare = a.compare.expanduser().resolve()
    print(f"gcloud identity: {check_identity()}")
    tf = {k: v["value"] for k, v in json.loads(run([TERRAFORM, f"-chdir={REPO / 'infra'}", "output", "-json"])).items()}
    if "batch_bucket" not in tf:
        raise SystemExit("infra/batch.tf is not applied yet (no batch_bucket output)")
    run_id = submit(a, tf)
    if a.no_wait:
        print(f"collect later: python3 -I infra/batch_submit.py --collect {run_id}")
        return 0
    wait(run_id, tf["batch_region"])
    return collect(run_id)


if __name__ == "__main__":
    sys.exit(main())
