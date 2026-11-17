"""fetch_paper.py offline: HTTP is replaced by canned responses, studies/ and inbox/ by tmp folders."""
import json

import pytest

import common
import fetch_paper as fp

PMID = "11111111"
DOI = "10.1000/test.1"
PDF = b"%PDF-1.5\n fake pdf body"


def resp(status=200, body=b"", ctype="application/pdf"):
    return {"status": status, "content_type": ctype, "final_url": "", "body": body,
            "error": "" if status == 200 else f"HTTP {status}"}


def epmc(pmcid="", oa="N"):
    rec = {"pmid": PMID, "doi": DOI, "pmcid": pmcid, "isOpenAccess": oa, "title": "A 16S study",
           "pubYear": "2020", "journalInfo": {"journal": {"title": "J Test"}}}
    return resp(body=json.dumps({"resultList": {"result": [rec]}}).encode(), ctype="application/json")


def unpaywall(*locs):
    return resp(body=json.dumps({"is_oa": bool(locs), "oa_status": "bronze", "oa_locations": list(locs),
                                 "best_oa_location": locs[0] if locs else None}).encode(), ctype="application/json")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STUDIES", tmp_path / "studies")
    monkeypatch.setattr(fp, "INBOX", tmp_path / "inbox")
    calls = []

    def install(routes):
        def fake_get(url, timeout=60):
            calls.append(url)
            for key, r in routes.items():
                if key in url:
                    return r
            raise AssertionError(f"unexpected request {url}")
        monkeypatch.setattr(fp, "http_get", fake_get)
    return tmp_path, calls, install


def log_lines(tmp):
    return [json.loads(x) for x in (tmp / "studies" / PMID / "log.jsonl").read_text().splitlines()]


def test_classify():
    assert fp.classify(resp(body=PDF)) == "pdf"
    assert fp.classify(resp(403, b"<html>Just a moment...</html>", "text/html")) == "blocked"
    assert fp.classify(resp(200, b"<html>Please complete the CAPTCHA</html>", "text/html")) == "blocked"
    assert fp.classify(resp(200, b"<html>article page</html>", "text/html")) == "landing_page"
    assert fp.classify(resp(404, b"", "text/html")) == "not_found"


def test_pmc_non_oa_is_never_requested_and_publisher_pdf_is_taken(env):
    tmp, calls, install = env
    pmc = {"host_type": "repository", "version": "acceptedVersion", "license": None,
           "url_for_pdf": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/pdf/x.pdf"}
    pub = {"host_type": "publisher", "version": "publishedVersion", "license": "cc-by",
           "url_for_pdf": "https://publisher.example/x.pdf"}
    install({"europepmc/webservices/rest/search": epmc(pmcid="PMC1", oa="N"),
             "api.unpaywall.org": unpaywall(pmc, pub), "publisher.example": resp(body=PDF)})
    res = fp.resolve_one(PMID)
    assert not any("pmc.ncbi" in c for c in calls)
    assert res["status"] == "fetched" and res["file"] == "sources/paper.pdf"
    assert res["license"] == "cc-by" and res["redistributable"] is True
    assert (tmp / "studies" / PMID / "sources" / "paper.pdf").read_bytes() == PDF
    assert [a["outcome"] for a in res["attempts"]] == ["skipped_pmc_non_oa", "pdf"]
    assert {line["step"] for line in log_lines(tmp)} == {"fulltext"}
    assert not any("email=" in line["source"] for line in log_lines(tmp))


def test_blocked_publisher_is_tried_once_and_left_for_a_person(env):
    tmp, calls, install = env
    pub = {"host_type": "publisher", "version": "publishedVersion", "license": None,
           "url_for_pdf": "https://publisher.example/x.pdf"}
    install({"europepmc/webservices/rest/search": epmc(),
             "api.unpaywall.org": unpaywall(pub),
             "publisher.example": resp(403, b"<html>cloudflare</html>", "text/html")})
    res = fp.resolve_one(PMID)
    assert res["status"] == "needs_human"
    assert sum("publisher.example" in c for c in calls) == 1
    assert res["attempts"][0]["outcome"] == "blocked"
    assert any(x["url"] == f"https://doi.org/{DOI}" for x in res["links_for_human"])
    assert "blocked" in {line["outcome"] for line in log_lines(tmp)}


def test_pmc_open_access_xml_first(env):
    tmp, calls, install = env
    xml = b'<?xml version="1.0"?><article><body>text</body></article>'
    install({"europepmc/webservices/rest/search": epmc(pmcid="PMC9", oa="Y"),
             "PMC9/fullTextXML": resp(body=xml, ctype="application/xml")})
    res = fp.resolve_one(PMID)
    assert res["status"] == "fetched" and res["route"] == "pmc_oa"
    assert not any("unpaywall" in c for c in calls)


def test_task_wont_do_and_ingest(env):
    tmp, calls, install = env
    install({"europepmc/webservices/rest/search": epmc(), "api.unpaywall.org": unpaywall()})
    fp.resolve_one(PMID)
    fp.open_task(PMID, 1, "human cases and controls")
    box = tmp / "inbox" / PMID
    assert "paper.pdf" in (box / "WANTED.md").read_text()
    assert PMID in (tmp / "inbox" / "README.md").read_text()

    (box / "paper.pdf").write_bytes(PDF)
    (box / "Table S1.xlsx").write_bytes(b"PK fake xlsx")
    fp.ingest([])
    src = tmp / "studies" / PMID / "sources"
    assert (src / "paper.pdf").read_bytes() == PDF
    assert (src / "supplements" / "Table S1.xlsx").exists()
    assert (box / "DONE.md").exists() and not (box / "WANTED.md").exists()
    assert not (box / "paper.pdf").exists()
    res = fp.load_result(PMID)
    assert res["status"] == "fetched_by_human"
    by_name = {f["name"]: f for f in res["human_files"]}
    assert by_name["paper.pdf"]["sha256"] == common.sha256_file(src / "paper.pdf")
    assert by_name["paper.pdf"]["dest"] == "sources/paper.pdf"
    last = log_lines(tmp)[-1]
    assert last["actor"] == fp.HUMAN and "sha256=" in last["detail"]


def test_wont_do_removes_empty_task(env):
    tmp, calls, install = env
    install({"europepmc/webservices/rest/search": epmc(), "api.unpaywall.org": unpaywall()})
    fp.resolve_one(PMID)
    fp.open_task(PMID, 3, "maybe")
    fp.wont_do(PMID, "mouse model")
    assert not (tmp / "inbox" / PMID).exists()
    assert fp.load_result(PMID)["status"] == "wont_do"
    assert fp.resolve_one(PMID)["status"] == "wont_do"     # not resolved again without --force
