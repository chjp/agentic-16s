"""Abstract screening (discovery/abstract_screening.py, llm_cli.py, sample_epmc.py), offline: no LLM, no network."""
import csv
import gzip
import json
import sys
from pathlib import Path

import abstract_screening as scr
import llm_cli
import sample_epmc

CRITERIA = Path(__file__).resolve().parents[1] / "criteria" / "ibd_criteria.txt"

# Haiku's real replies for two DSS mouse papers in ibd100 (v1 prompt): INCLUDE, with a reason that excludes.
MOUSE_REASONS = [
    "The abstract describes a DSS-induced mouse model of ulcerative colitis, which is an animal study with no human "
    "participants, so it fails inclusion criterion 1 and should be excluded.",
    "The study uses a DSS-induced ulcerative colitis model with 16S rRNA sequencing of gut microbiota, comparing "
    "GAS-treated versus untreated groups, which fits the criteria. However, the abstract describes animal experiments "
    "and does not explicitly confirm human participants, so this may fail inclusion criterion 1 and exclusion criterion 1.",
]


def test_include_whose_reason_argues_for_exclusion_is_caught():
    for reason in MOUSE_REASONS:
        assert scr.contradicts_include(reason)
    assert not scr.contradicts_include("Stool 16S from 40 UC patients and 20 controls; no exclusion criteria apply.")


def test_screen_one_sends_a_contradicted_include_to_review(monkeypatch, tmp_path):
    monkeypatch.setattr(scr, "call_llm", lambda *a, **k: ({"reason": MOUSE_REASONS[0], "decision": "INCLUDE"},
                                                          {"model": "haiku", "cost_usd": 0}))
    paper = scr.screen_one({"pmid": "39914063", "title": "t", "abstract": "a"}, "c", "haiku", "claude", tmp_path, 10)
    assert paper["screening_decision"] == "UNCERTAIN"
    assert paper["screening_reason"].startswith("[INCLUDE contradicted by its own reason")


def test_prompt_asks_for_the_reason_before_the_decision():
    p = scr.build_screening_prompt("title", "abstract", "criteria")
    assert p.index('"reason"') < p.index('"decision"')


def test_ibd_criteria_require_stool():
    text = CRITERIA.read_text()
    assert "stool" in text.lower() and "No stool samples" in text


def test_stored_reply_is_reused_only_for_the_same_prompt(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(llm_cli, "_run_claude", lambda prompt, model, timeout: calls.append(prompt) or
                        {"text": '{"ok": 1}', "model": model, "cost_usd": 0, "seconds": 0})
    llm_cli.call_llm("A", "screening", "1", raw_dir=tmp_path)
    llm_cli.call_llm("A", "screening", "1", raw_dir=tmp_path)   # cached
    llm_cli.call_llm("B", "screening", "1", raw_dir=tmp_path)   # criteria changed: ask again
    assert calls == ["A", "B"]
    f = tmp_path / "screening" / "1.json"
    meta = json.loads(f.read_text())
    meta.pop("prompt_sha256")
    f.write_text(json.dumps(meta))
    llm_cli.call_llm("C", "screening", "1", raw_dir=tmp_path)   # saved before hashing existed: kept
    assert calls == ["A", "B"]


def test_sample_epmc_leaves_out_earlier_batches(monkeypatch, tmp_path):
    pool = tmp_path / "pool.jsonl.gz"
    with gzip.open(pool, "wt") as fh:
        for i in range(1, 21):
            fh.write(json.dumps({"pmid": str(i), "abstractText": "x", "title": "t"}) + "\n")
    first = tmp_path / "first.tsv"
    first.write_text("pmid\n" + "".join(f"{i}\n" for i in range(1, 11)))
    out = tmp_path / "second.tsv"
    monkeypatch.setattr(sys, "argv", ["sample_epmc", "-i", str(pool), "-n", "10", "--seed", "17",
                                      "--exclude", str(first), "-o", str(out)])
    sample_epmc.main()
    got = {r["pmid"] for r in csv.DictReader(open(out), delimiter="\t")}
    assert got == {str(i) for i in range(11, 21)}
