"""db/load_study.py: where paper metadata and full-text provenance come from (no database needed)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import load_study as ls  # noqa: E402

HEADER = "pmid\tpmc_id\tdoi\ttitle\tjournal\tpub_date\tis_open_access\tabstract\n"


def write(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_paper_meta_searches_every_run_in_name_order(tmp_path):
    write(tmp_path / "ibd100/papers.tsv", HEADER + "111\tPMC1\t\tfirst batch\tJ\t2020\tY\tabs\n")
    write(tmp_path / "ibd200b/papers.tsv", HEADER + "222\t\t\tsecond batch\tJ\t2022\tN\tabs\n"
                                                    "111\t\t\tduplicate\tJ\t2020\tY\tabs\n")
    assert ls.paper_meta("222", tmp_path)["title"] == "second batch"
    assert ls.paper_meta("111", tmp_path)["title"] == "first batch"  # the earlier run wins
    assert ls.paper_meta("999", tmp_path) == {}


def test_study_curated_before_fetch_paper_is_pmc_open_access(tmp_path):
    write(tmp_path / "sources/fulltext.xml")
    write(tmp_path / "sources/supplements/Table S1.xlsx")
    status, docs = ls.fulltext_sources(tmp_path)
    assert status == "fetched_oa"
    assert docs == [("sources/fulltext.xml", "fulltext_xml", "pmc", "agent"),
                    ("sources/supplements/Table S1.xlsx", "supplement", "europepmc", "agent")]


def test_owner_supplied_pdf_and_supplement_are_human_uploads(tmp_path):
    write(tmp_path / "sources/paper.pdf", "%PDF-")
    write(tmp_path / "sources/supplements/mmc1.xlsx")
    write(tmp_path / "sources/supplements/agent_found.csv")
    write(tmp_path / "fetch_result.json", json.dumps({
        "status": "fetched_by_human", "route": "human_upload",
        "human_files": [{"name": "paper.pdf", "dest": "sources/paper.pdf"},
                        {"name": "mmc1.xlsx", "dest": "sources/supplements/mmc1.xlsx"}]}))
    status, docs = ls.fulltext_sources(tmp_path)
    assert status == "human_supplied"
    assert ("sources/paper.pdf", "fulltext_pdf", "human_upload", "human") in docs
    assert ("sources/supplements/mmc1.xlsx", "supplement", "human_upload", "human") in docs
    assert ("sources/supplements/agent_found.csv", "supplement", "europepmc", "agent") in docs


def test_free_publisher_pdf_fetched_by_script(tmp_path):
    write(tmp_path / "sources/paper.pdf", "%PDF-")
    write(tmp_path / "fetch_result.json", json.dumps({"status": "fetched", "route": "unpaywall:publisher"}))
    assert ls.fulltext_sources(tmp_path) == ("fetched_oa", [("sources/paper.pdf", "fulltext_pdf", "publisher", "script")])


def test_no_full_text(tmp_path):
    assert ls.fulltext_sources(tmp_path) == ("not_tried", [])
    write(tmp_path / "fetch_result.json", json.dumps({"status": "wont_do"}))
    assert ls.fulltext_sources(tmp_path) == ("unavailable", [])
