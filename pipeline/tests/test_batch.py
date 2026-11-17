"""Google Batch runner (infra/batch_submit.py) and table comparison (compare_tables.py)."""
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import compare_tables

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("batch_submit", REPO / "infra" / "batch_submit.py")
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


def cfg(**kw):
    args = dict(runner_text="#!/bin/bash\necho hi\n", env={"MODE": "run"}, machine="e2-standard-8",
                region="us-central1", network="projects/p/global/networks/n",
                subnetwork="projects/p/regions/us-central1/subnetworks/n", service_account="sa@p.iam",
                max_min=360, disk_gb=50, labels={"app": "agentic16s"})
    args.update(kw)
    return bs.job_config(**args)


def test_job_config_standard_vm_debian_and_time_limit():
    c = cfg()
    policy = c["allocationPolicy"]["instances"][0]["policy"]
    assert policy["provisioningModel"] == "STANDARD"  # credits don't pay for Spot
    assert policy["bootDisk"]["image"] == "batch-debian"  # Batch supports Debian/Rocky only
    assert c["allocationPolicy"]["location"]["allowedLocations"] == ["regions/us-central1"]
    spec = c["taskGroups"][0]["taskSpec"]
    assert spec["maxRetryCount"] == 0 and spec["maxRunDuration"] == f"{400 * 60}s"
    assert c["logsPolicy"]["destination"] == "CLOUD_LOGGING"
    assert c["allocationPolicy"]["network"]["networkInterfaces"][0]["noExternalIpAddress"] is False


def test_job_config_rejects_machines_outside_the_credit_list():
    try:
        cfg(machine="c3-standard-8")
    except ValueError:
        return
    raise AssertionError("c3 accepted")


def test_code_snapshot_is_deterministic_and_skips_scratch(tmp_path):
    (tmp_path / "pipeline" / "bin" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pipeline" / "bin" / "x.py").write_text("print(1)\n")
    (tmp_path / "pipeline" / "bin" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (tmp_path / "studies" / "1" / "work").mkdir(parents=True)
    (tmp_path / "studies" / "1" / "log.jsonl").write_text("{}\n")
    (tmp_path / "studies" / "1" / "work" / "scratch.txt").write_text("x")
    (tmp_path / "studies" / "2").mkdir()
    (tmp_path / "studies" / "2" / "other.txt").write_text("y")
    a, b = bs.code_snapshot(tmp_path, "1"), bs.code_snapshot(tmp_path, "1")
    assert a == b
    names = tarfile.open(fileobj=io.BytesIO(a), mode="r:gz").getnames()
    assert sorted(names) == ["pipeline/bin/x.py", "studies/1/log.jsonl"]


def test_inputs_archive_layout(tmp_path):
    (tmp_path / "manifest.tsv").write_text("run_accession\nSRR1\n")
    (tmp_path / "runs.csv").write_text("SRR1\n")
    names = tarfile.open(fileobj=io.BytesIO(bs.inputs_archive("9", {"main": tmp_path})), mode="r:gz").getnames()
    assert sorted(names) == ["pipeline/9/main/manifest.tsv", "pipeline/9/main/runs.csv"]


def test_new_log_lines_takes_only_what_the_job_appended():
    vm = '{"a": 1}\n{"a": 2}\n{"a": 3}\n'
    out = [json.loads(x) for x in bs.new_log_lines(2, vm, "batch:j")]
    assert out == [{"a": 3, "host": "batch:j"}]


def test_merge_profile_appends_new_runs_once():
    old = {"trace": "t1", "started_at": "s1"}
    new = {"trace": "t2", "started_at": "s2"}
    local = {"pmid": "1", "runs": [dict(old)]}
    merged = bs.merge_profile(local, {"pmid": "1", "runs": [old, new]}, {("t1", "s1")}, "batch:j")
    assert [r["trace"] for r in merged["runs"]] == ["t1", "t2"] and merged["runs"][1]["host"] == "batch:j"
    again = bs.merge_profile(merged, {"pmid": "1", "runs": [old, new]}, {("t1", "s1")}, "batch:j")
    assert len(again["runs"]) == 2


def test_status_times_reads_events():
    job = {"status": {"state": "SUCCEEDED", "runDuration": "4321.5s", "statusEvents": [
        {"description": "Job state is set from QUEUED to SCHEDULED for job x.", "eventTime": "t0"},
        {"description": "Job state is set from SCHEDULED to RUNNING for job x.", "eventTime": "t1"},
        {"description": "Job state is set from RUNNING to SUCCEEDED for job x.", "eventTime": "t2"}]}}
    t = bs.status_times(job)
    assert t == {"state": "SUCCEEDED", "run_duration_s": 4321.5, "running_at": "t1", "finished_at": "t2"}


def test_merge_studies_only_takes_what_the_job_changed(tmp_path, monkeypatch):
    local, vm = tmp_path / "local", tmp_path / "vm"
    for d in (local, vm):
        d.mkdir()
        (d / "log.jsonl").write_text('{"x": 0}\n')
        (d / "pipeline_qc_subset6.json").write_text('{"old": true}\n')  # untouched by a full run
    (vm / "log.jsonl").write_text('{"x": 0}\n{"x": 1}\n')
    (vm / "pipeline_qc.json").write_text('{"new": true}\n')
    before = {"log.jsonl": b'{"x": 0}\n', "pipeline_qc_subset6.json": b'{"old": true}\n'}
    monkeypatch.setattr(bs, "REPO", tmp_path)
    st = {"snapshot_log_lines": 1, "snapshot_profile_keys": [], "compare": "/ref"}
    bs.merge_studies(st, vm, local, "batch:j", before)
    assert (local / "pipeline_qc_batch.json").read_text() == '{"new": true}\n'
    assert not (local / "pipeline_qc_subset6_batch.json").exists()
    assert len((local / "log.jsonl").read_text().splitlines()) == 2


def write_run(d: Path, counts: dict, genus: dict, params: dict):
    t = d / "tables"
    t.mkdir(parents=True)
    (d / "params.json").write_text(json.dumps({"params": {k: {"value": v} for k, v in params.items()}}))
    (t / "run_qc.tsv").write_text("run_accession\treads_input\treads_nonchim\tstatus\nR1\t100\t80\tok\n")
    (t / "asv.tsv").write_text("sequence_md5\tsequence\tlength\n" + "".join(f"{k}\tACGT\t4\n" for k in counts))
    (t / "abundance.tsv").write_text("run_accession\tsequence_md5\tcount\n" + "".join(f"R1\t{k}\t{v}\n" for k, v in counts.items()))
    (t / "taxonomy_assignment.tsv").write_text("sequence_md5\tgenus\tspecies\n" + "".join(f"{k}\t{g}\t\n" for k, g in genus.items()))


def test_compare_tables_identical_counts_and_genus_difference(tmp_path):
    write_run(tmp_path / "ref", {"a": 50, "b": 30}, {"a": "Bacteroides", "b": ""}, {"seed": 100, "skip_dada_addspecies": None})
    write_run(tmp_path / "new", {"a": 50, "b": 30}, {"a": "Bacteroides", "b": "Roseburia"}, {"seed": 100, "skip_dada_addspecies": True})
    res = compare_tables.compare(tmp_path / "ref", tmp_path / "new")
    assert res["counts_identical"]
    assert list(res["params_differ"]) == ["skip_dada_addspecies"]
    assert res["taxonomy"]["label_differs"]["genus"] == 1
    assert res["taxonomy"]["label_differs"]["species"].startswith("not compared")


def test_compare_tables_flags_a_changed_count(tmp_path):
    write_run(tmp_path / "ref", {"a": 50}, {"a": "X"}, {})
    write_run(tmp_path / "new", {"a": 51}, {"a": "X"}, {})
    res = compare_tables.compare(tmp_path / "ref", tmp_path / "new")
    assert not res["counts_identical"] and res["abundance"]["cells_differ"] == 1
