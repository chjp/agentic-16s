"""resource_monitor: ps sampling of Nextflow tasks, joined with the trace by hash."""
import resource_monitor as rm

WD = "/data/nf-work/x/ampliseq/88/213f9f83035c634242932afe63ee87"


def fake_procs():
    # pid: (ppid, rss_kb, cpu, command)
    return {
        100: (1, 500_000, 5.0, "/usr/bin/java -jar nextflow"),
        200: (100, 2_000, 0.0, "/bin/bash -ue .command.run"),
        201: (200, 1_000, 0.0, f"bash -C -e -u -o pipefail {WD}/.command.run nxf_trace"),
        202: (201, 6_000_000, 99.0, "/env/lib/R/bin/exec/R --no-echo --file=.command.sh"),
        300: (100, 1_000, 0.0, "/bin/bash -ue .command.run"),
        999: (1, 9_000_000, 50.0, "unrelated"),
    }


def test_measure_groups_by_task_tree():
    m = rm.measure(100, fake_procs())
    assert m["rss_kb"] == 500_000 + 2_000 + 1_000 + 6_000_000 + 1_000  # unrelated pid 999 left out
    t = m["tasks"][200]
    assert t["hash"] == "88/213f9f" and t["rss_kb"] == 6_003_000 and t["cpu_pct"] == 99.0
    assert m["tasks"][300]["hash"] is None
    assert rm.measure(12345, fake_procs()) is None


def test_parsers():
    assert rm.parse_duration("46ms") == 0.046
    assert rm.parse_duration("1h 2m 3s") == 3723
    assert rm.parse_duration("-") is None
    assert rm.parse_mem_gb("512 MB") == 0.512
    assert rm.parse_mem_gb("-") is None
    assert rm.short_name("NFCORE_AMPLISEQ:AMPLISEQ:DADA2_TAXONOMY_WF:DADA2_ADDSPECIES (x.rds,y)") == "DADA2_ADDSPECIES"


def test_summarize_joins_samples_and_skips_cached(tmp_path):
    s = rm.Sampler(100, tmp_path / "s.json")
    s.add(rm.measure(100, fake_procs()))
    trace = [
        {"name": "A:DADA2_ADDSPECIES (1)", "hash": "88/213f9f", "status": "FAILED", "realtime": "9m 30s",
         "peak_rss": "-", "%cpu": "-"},
        {"name": "A:FASTQC (r1)", "hash": "aa/bbbbbb", "status": "COMPLETED", "realtime": "20s",
         "peak_rss": "300 MB", "%cpu": "150.0%"},
        {"name": "A:FASTQC (r2)", "hash": "cc/dddddd", "status": "CACHED", "realtime": "1h", "peak_rss": "-", "%cpu": "-"},
    ]
    out = rm.summarize(trace, s.data)
    assert list(out) == ["DADA2_ADDSPECIES", "FASTQC"]  # heaviest first
    a = out["DADA2_ADDSPECIES"]
    assert a["failed"] == 1 and a["realtime_max_min"] == 9.5 and 6.1 < a["peak_rss_gb"] < 6.2
    f = out["FASTQC"]
    assert f["tasks"] == 1 and f["peak_rss_gb"] == 0.3 and f["peak_cpu_pct"] == 150


def test_sampler_stop_and_join(tmp_path):
    """start/stop/join must work on every Python: 3.12+ Thread.join() calls self._stop(), which an
    attribute of that name used to shadow (crashed the first GCE run, 2026-11-15)."""
    import os
    s = rm.Sampler(os.getpid(), tmp_path / "s.json", interval=0.05)
    s.start()
    s.stop()
    s.join(timeout=10)
    assert not s.is_alive()
