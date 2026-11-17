"""resources.config: the addSpecies chunk size has to fit the memory cap (measured on 23013615)."""
import run_study


def test_addspecies_chunk_fits_measured_memory():
    for mem_gb in (8, 16, 32):
        n = run_study.addspecies_chunk(mem_gb)
        peak = run_study.SPECIES_REFS * (3430 + 23.3 * n)
        assert peak <= 0.9 * mem_gb * (1 << 30)
    assert run_study.addspecies_chunk(8) < 200  # n = 200 ran out of R's 16 GB on 2026-11-15


def test_resource_config_overrides_addspecies(tmp_path):
    text = run_study.resource_config(tmp_path).read_text()
    assert "withName: DADA2_ADDSPECIES" in text
    assert f"'n = {run_study.addspecies_chunk(8)}'" in text or "'n = " in text
    assert "tryRC = TRUE" in text


def test_addspecies_runs_chunks_in_parallel_on_big_machines():
    assert run_study.addspecies_slots(8) == 1      # the Mac: unchanged
    assert run_study.addspecies_slots(30) == 2     # e2-standard-8 under Docker: 15 GB per chunk
    assert run_study.addspecies_slots(46) == 3


def test_addspecies_memory_grows_on_retry(tmp_path, monkeypatch):
    """A fixed memory value disabled nf-core's retry with more memory (37661264, 2026-11-16)."""
    monkeypatch.setattr(run_study.common, "versions", lambda: {"NF_PROFILE": "docker"})

    class Out:
        stdout = f"8 {31 << 30}"
    monkeypatch.setattr(run_study.subprocess, "run", lambda *a, **k: Out())
    text = run_study.resource_config(tmp_path).read_text()
    assert "memory = { [15.GB * task.attempt, 30.GB].min() }" in text
