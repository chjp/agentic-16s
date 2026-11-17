#!/usr/bin/env python3
"""Find a legal full-text copy of a paper, or hand the paper to a person. Standard library only.

  python3 -I pipeline/bin/fetch_paper.py resolve 26982597 33010388
  python3 -I pipeline/bin/fetch_paper.py resolve --from-tsv papers_fulltext.tsv --status NO_PMC,NOT_OA
  python3 -I pipeline/bin/fetch_paper.py open-task 33010388 --priority 1 --reason "saliva, 22 IBD vs 8 controls"
  python3 -I pipeline/bin/fetch_paper.py wont-do 39914063 --reason "mouse DSS model, no human subjects"
  python3 -I pipeline/bin/fetch_paper.py ingest          # every inbox folder that holds files
  python3 -I pipeline/bin/fetch_paper.py inbox           # rewrite inbox/README.md

resolve tries, in order, and stops at the first copy it gets:
  1. PMC open-access XML (Europe PMC fullTextXML)             -> studies/<PMID>/sources/fulltext.xml
  2. free PDFs listed by Unpaywall and Europe PMC, one plain GET each, accepted only if the body is a
     PDF                                                       -> studies/<PMID>/sources/paper.pdf
It never downloads a non-open-access article from PMC or Europe PMC (their terms allow automated
retrieval of the open-access subset only), never retries a blocked request, and never solves a bot
check. What it cannot get becomes a human task in inbox/<PMID>/WANTED.md (open-task); files the owner
puts there are moved into studies/<PMID>/sources/ with their sha256 (ingest).

Every attempt is a line in studies/<PMID>/log.jsonl; the outcome is studies/<PMID>/fetch_result.json.
The judgment calls (is this paper worth a person's time, which priority) belong to the agent that runs
this script: see .claude/skills/fetch-paper/SKILL.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules; python3 -I drops this
import common  # noqa: E402

ACTOR = "script:pipeline/bin/fetch_paper.py"
HUMAN = "human:owner"
INBOX = common.REPO / "inbox"
USER_AGENT = "agentic-16s/0.1 (open-access full-text resolver)"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL = "https://api.unpaywall.org/v2/"
DEFAULT_CONTACT = "agentic-16s@example.org"   # Unpaywall asks for an e-mail; never a personal one
MAX_BYTES = 100 * 1024 * 1024
MIN_INTERVAL = 1.0                            # seconds between requests to the same host
DONE_STATES = {"fetched", "fetched_by_human", "wont_do"}
INBOX_META = {"WANTED.md", "DONE.md", ".DS_Store"}
PMC_HOSTS = ("ncbi.nlm.nih.gov", "europepmc.org", "ebi.ac.uk")
BLOCK_WORDS = re.compile(rb"captcha|cloudflare|just a moment|access denied|are you a robot|verify you are human",
                         re.I)

_last_hit: dict[str, float] = {}
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


# ---------------------------------------------------------------- HTTP

def http_get(url: str, timeout: int = 60) -> dict:
    """One GET, never raises. Returns status, content type, final URL and body (capped)."""
    host = urllib.parse.urlsplit(url).netloc
    wait = MIN_INTERVAL - (time.time() - _last_hit.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return {"status": r.status, "content_type": r.headers.get("Content-Type", ""),
                    "final_url": r.geturl(), "body": r.read(MAX_BYTES), "error": ""}
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(200_000)
        except OSError:
            pass
        return {"status": e.code, "content_type": e.headers.get("Content-Type", "") if e.headers else "",
                "final_url": url, "body": body, "error": f"HTTP {e.code}"}
    except (OSError, ValueError) as e:
        return {"status": 0, "content_type": "", "final_url": url, "body": b"", "error": str(e)[:200]}


def classify(resp: dict) -> str:
    """pdf | xml | blocked | not_found | landing_page | error"""
    body, status = resp["body"], resp["status"]
    if status == 200 and body.startswith(b"%PDF"):
        return "pdf"
    if status == 200 and body.lstrip()[:5] == b"<?xml" and b"<article" in body[:5000]:
        return "xml"
    if status in (401, 402, 403, 429, 503) or BLOCK_WORDS.search(body[:50_000]):
        return "blocked"
    if status in (404, 410):
        return "not_found"
    if status == 200:
        return "landing_page"
    return "error"


def strip_query(url: str) -> str:
    """For logs: drop the query string, which may carry a contact e-mail."""
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def contact_email() -> str:
    return (os.environ.get("UNPAYWALL_EMAIL") or common._read_env_file(common.REPO / ".env").get("UNPAYWALL_EMAIL")
            or DEFAULT_CONTACT)


# ---------------------------------------------------------------- paths and state

def study_dir(pmid: str) -> Path:
    d = common.STUDIES / pmid
    (d / "sources" / "fetch").mkdir(parents=True, exist_ok=True)
    return d


def result_path(pmid: str) -> Path:
    return common.STUDIES / pmid / "fetch_result.json"


def load_result(pmid: str) -> dict | None:
    p = result_path(pmid)
    return json.loads(p.read_text()) if p.exists() else None


def save_result(pmid: str, res: dict) -> None:
    common.write_json(result_path(pmid), res)


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def log(pmid: str, action: str, outcome: str, detail: str, source: str = "", files=None, actor: str = ACTOR):
    common.log_event(pmid, action, outcome, detail, source=strip_query(source) if source else "", files=files,
                     step="fulltext", actor=actor)


def is_pmc_host(url: str) -> bool:
    host = urllib.parse.urlsplit(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in PMC_HOSTS)


# ---------------------------------------------------------------- resolve

def europepmc_record(pmid: str, sdir: Path) -> dict:
    q = urllib.parse.quote(f"EXT_ID:{pmid} AND SRC:MED")
    url = f"{EPMC}/search?query={q}&resultType=core&format=json"
    resp = http_get(url)
    if resp["status"] != 200:
        log(pmid, "Europe PMC metadata", "failure", resp["error"] or f"HTTP {resp['status']}", url)
        return {}
    (sdir / "sources" / "fetch" / "europepmc_core.json").write_bytes(resp["body"])
    hits = json.loads(resp["body"]).get("resultList", {}).get("result", [])
    rec = hits[0] if hits else {}
    log(pmid, "Europe PMC metadata", "success" if rec else "failure",
        f"pmcid={rec.get('pmcid', '')} isOpenAccess={rec.get('isOpenAccess', '')} doi={rec.get('doi', '')}"
        if rec else "no record for this PMID", url, ["sources/fetch/europepmc_core.json"])
    return rec


def unpaywall_record(pmid: str, doi: str, sdir: Path) -> dict:
    url = UNPAYWALL + urllib.parse.quote(doi) + "?email=" + urllib.parse.quote(contact_email())
    resp = http_get(url)
    if resp["status"] != 200:
        log(pmid, "Unpaywall lookup", "failure", resp["error"] or f"HTTP {resp['status']}", url)
        return {}
    (sdir / "sources" / "fetch" / "unpaywall.json").write_bytes(resp["body"])
    rec = json.loads(resp["body"])
    log(pmid, "Unpaywall lookup", "success",
        f"is_oa={rec.get('is_oa')} oa_status={rec.get('oa_status')} locations={len(rec.get('oa_locations') or [])}",
        url, ["sources/fetch/unpaywall.json"])
    return rec


def candidates(epmc: dict, upw: dict) -> tuple[list[dict], list[dict]]:
    """PDF links worth one GET, and other links (landing pages, datasets) for a person to look at."""
    pdfs: list[dict] = []
    others: list[dict] = []
    seen: set[str] = set()
    locs = list(upw.get("oa_locations") or [])
    best = upw.get("best_oa_location")
    if best:
        locs = [best] + [loc for loc in locs if loc != best]
    for loc in locs:
        pdf, page = loc.get("url_for_pdf"), loc.get("url_for_landing_page") or loc.get("url")
        item = {"source": "unpaywall", "host_type": loc.get("host_type"), "version": loc.get("version"),
                "license": loc.get("license")}
        if pdf and pdf not in seen:
            seen.add(pdf)
            pdfs.append({**item, "url": pdf})
        elif page and page not in seen:
            seen.add(page)
            others.append({**item, "url": page})
    for u in (epmc.get("fullTextUrlList") or {}).get("fullTextUrl", []):
        url = u.get("url", "")
        if not url or url in seen or u.get("availability") not in ("Free", "Open access"):
            continue
        seen.add(url)
        item = {"source": "europepmc", "host_type": u.get("site"), "version": None, "license": None, "url": url}
        (pdfs if u.get("documentStyle") == "pdf" else others).append(item)
    return pdfs, others


def resolve_one(pmid: str, doi_hint: str = "", force: bool = False) -> dict:
    old = load_result(pmid) or {}
    if old.get("status") in DONE_STATES and not force:
        print(f"{pmid}: {old['status']} already (use --force to resolve again)")
        return old
    sdir = study_dir(pmid)
    epmc = europepmc_record(pmid, sdir)
    doi = epmc.get("doi") or doi_hint
    pmcid = epmc.get("pmcid", "")
    is_oa = epmc.get("isOpenAccess") == "Y"
    res = {
        "pmid": pmid, "doi": doi, "pmcid": pmcid, "title": epmc.get("title", ""),
        "journal": (epmc.get("journalInfo") or {}).get("journal", {}).get("title", ""),
        "year": epmc.get("pubYear", ""), "is_open_access": is_oa,
        "status": "needs_human", "route": None, "file": None, "license": None, "version": None,
        "redistributable": False, "attempts": [], "links_for_human": [], "checked_at": now(),
    }
    if old.get("task"):
        res["task"] = old["task"]

    def attempt(cand: dict, kind: str) -> str:
        resp = http_get(cand["url"])
        verdict = classify(resp)
        res["attempts"].append({**cand, "kind": kind, "http": resp["status"], "content_type": resp["content_type"],
                                "final_url": strip_query(resp["final_url"]), "outcome": verdict,
                                "error": resp["error"]})
        ok = verdict == ("xml" if kind == "xml" else "pdf")
        name = "fulltext.xml" if kind == "xml" else "paper.pdf"
        if ok:
            (sdir / "sources" / name).write_bytes(resp["body"])
            log(pmid, f"download {kind}", "success", f"HTTP {resp['status']}, {len(resp['body'])} bytes, "
                f"{cand.get('host_type')} {cand.get('version') or ''} license={cand.get('license')}",
                cand["url"], [f"sources/{name}"])
        else:
            outcome = "blocked" if verdict == "blocked" else "failure"
            log(pmid, f"download {kind}", outcome, f"{verdict}: HTTP {resp['status']} {resp['content_type']} "
                f"{resp['error']}".strip(), cand["url"])
        return name if ok else ""

    # 1. PMC open-access XML
    if pmcid and is_oa:
        got = attempt({"source": "europepmc", "host_type": "pmc_oa", "version": "publishedVersion",
                       "license": epmc.get("license"), "url": f"{EPMC}/{pmcid}/fullTextXML"}, "xml")
        if got:
            res.update(status="fetched", route="pmc_oa", file=f"sources/{got}", license=epmc.get("license"))

    # 2. free PDFs listed by Unpaywall / Europe PMC
    upw = unpaywall_record(pmid, doi, sdir) if doi and res["status"] != "fetched" else {}
    pdfs, others = candidates(epmc, upw)
    if res["status"] != "fetched":
        for cand in pdfs:
            if is_pmc_host(cand["url"]) and not is_oa:
                res["attempts"].append({**cand, "kind": "pdf", "http": None, "outcome": "skipped_pmc_non_oa",
                                        "error": "PMC/Europe PMC allow automated retrieval of the OA subset only"})
                log(pmid, "download pdf", "skipped", "PMC copy is not in the open-access subset; a person can "
                    "read and save it", cand["url"])
                others.append(cand)
                continue
            got = attempt(cand, "pdf")
            if got:
                lic = cand.get("license")
                res.update(status="fetched", route=f"{cand['source']}:{cand.get('host_type')}",
                           file=f"sources/{got}", license=lic, version=cand.get("version"),
                           redistributable=bool(lic and lic.startswith("cc")))
                break

    res["free_to_read"] = [{k: o.get(k) for k in ("source", "host_type", "version", "license", "url")}
                           for o in others]
    links = []
    if doi:
        links.append({"what": "publisher (DOI)", "url": f"https://doi.org/{doi}"})
    if pmcid:
        links.append({"what": "PubMed Central, free to read: save the PDF from the page",
                      "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"})
    for o in others + [a for a in res["attempts"] if a.get("outcome") in ("blocked", "landing_page")]:
        if o["url"] not in {x["url"] for x in links}:
            links.append({"what": f"{o.get('host_type') or o['source']} {o.get('version') or ''}".strip()
                          + (f", {o['license']}" if o.get("license") else ""), "url": o["url"]})
    res["links_for_human"] = links
    save_result(pmid, res)
    tried = ", ".join(f"{a['outcome']}" for a in res["attempts"]) or \
        ("free to read on a web page only" if res["free_to_read"] else "no free copy listed")
    print(f"{pmid}: {res['status']}" + (f" via {res['route']}" if res["route"] else f" ({tried})"))
    return res


def pmids_from_tsv(path: Path, statuses: set[str]) -> list[tuple[str, str]]:
    rows = common.read_tsv(path)
    return [(r["pmid"], r.get("doi", "")) for r in rows
            if not statuses or r.get("download_status", "").strip() in statuses]


# ---------------------------------------------------------------- human tasks

def wanted_text(res: dict) -> str:
    t = res["task"]
    tried = [f"- {a['outcome']}: {a['url']} (HTTP {a.get('http')})" for a in res["attempts"]]
    tried += [f"- listed as free to read, but no direct PDF link a script may fetch: {o['url']}"
              + (f" ({o['license']})" if o.get("license") else "")
              for o in res.get("free_to_read", []) if o["url"] not in {a["url"] for a in res["attempts"]}]
    tried = "\n".join(tried) or "- no free copy is listed by Unpaywall or Europe PMC"
    links = "\n".join(f"- {x['what']}: {x['url']}" for x in res["links_for_human"])
    return f"""# {res['pmid']}: {res['title']}

{res['journal']} ({res['year']}), DOI {res['doi'] or 'none'}

**Priority {t['priority']}** (1 = most useful). Why it is worth getting: {t['reason']}

## What to put in this folder

1. The article as **`paper.pdf`** (exactly this name).
2. Every supplementary file (xlsx, docx, csv, pdf, zip) with its original name. Sample tables matter most.

Only copies you can legally get: open access, your institution's subscription, or the authors.
Then tell Claude "ingest the inbox", or run `python3 -I pipeline/bin/fetch_paper.py ingest {res['pmid']}`.

## Where to look

{links}

## What the resolver tried ({res['checked_at']})

{tried}
"""


def open_task(pmid: str, priority: int, reason: str) -> None:
    res = load_result(pmid)
    if not res:
        sys.exit(f"{pmid}: run resolve first")
    if res["status"] in ("fetched", "fetched_by_human"):
        sys.exit(f"{pmid}: already {res['status']}, no task needed")
    res["status"] = "needs_human"
    res["task"] = {"priority": priority, "reason": reason, "opened_at": now()}
    save_result(pmid, res)
    d = INBOX / pmid
    d.mkdir(parents=True, exist_ok=True)
    (d / "WANTED.md").write_text(wanted_text(res))
    log(pmid, "open human task: obtain full text", "blocked",
        f"priority {priority}; {reason}; no automatic legal copy", files=[f"inbox/{pmid}/WANTED.md"])
    print(f"{pmid}: task opened in inbox/{pmid}/")
    write_inbox_index()


def wont_do(pmid: str, reason: str) -> None:
    res = load_result(pmid)
    if not res:
        sys.exit(f"{pmid}: run resolve first")
    d = INBOX / pmid
    if d.exists():
        extra = [p for p in d.iterdir() if p.name not in INBOX_META]
        if extra:
            sys.exit(f"{pmid}: inbox/{pmid} holds files; ingest or remove them first")
        shutil.rmtree(d)
    res["status"] = "wont_do"
    res["task"] = {**(res.get("task") or {}), "wont_do_reason": reason, "closed_at": now()}
    save_result(pmid, res)
    log(pmid, "skip: not worth a person's time", "skipped", reason)
    print(f"{pmid}: wont_do")
    write_inbox_index()


def inbox_files(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if p.name not in INBOX_META and not p.name.startswith("."))


def ingest_one(pmid: str) -> bool:
    d = INBOX / pmid
    files = inbox_files(d) if d.exists() else []
    if not files:
        return False
    sdir = study_dir(pmid)
    supp = sdir / "sources" / "supplements"
    pdfs = [f for f in files if f.is_file() and f.suffix.lower() == ".pdf"]
    paper = next((f for f in files if f.name.lower() == "paper.pdf"), None)
    note = ""
    if paper is None and len(pdfs) == 1:
        paper, note = pdfs[0], f"no file named paper.pdf; the only PDF ({pdfs[0].name}) taken as the article"
    moved = []
    for f in files:
        dest = sdir / "sources" / "paper.pdf" if f == paper else supp / f.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            sys.exit(f"{pmid}: {dest.relative_to(common.REPO)} exists already; move it away first")
        shutil.move(str(f), str(dest))
        moved.append({"name": f.name, "dest": str(dest.relative_to(sdir)),
                      "sha256": common.sha256_file(dest) if dest.is_file() else None,
                      "bytes": dest.stat().st_size if dest.is_file() else None,
                      "is_pdf": dest.is_file() and dest.read_bytes()[:4] == b"%PDF"})
    res = load_result(pmid) or {"pmid": pmid, "attempts": [], "links_for_human": []}
    res.setdefault("human_files", []).extend({**m, "ingested_at": now(), "origin": "human_upload"} for m in moved)
    if paper is not None:
        res.update(status="fetched_by_human", route="human_upload", file="sources/paper.pdf",
                   license=None, redistributable=False)
    save_result(pmid, res)
    log(pmid, "supplied files via inbox", "success" if paper is not None else "partial",
        "; ".join(f"{m['dest']} sha256={m['sha256']}" for m in moved) + (f"; {note}" if note else "")
        + ("" if paper is not None else "; no article PDF yet"),
        source=f"inbox/{pmid}", files=[m["dest"] for m in moved], actor=HUMAN)
    wanted = d / "WANTED.md"
    if paper is not None and wanted.exists():
        wanted.rename(d / "DONE.md")
        with open(d / "DONE.md", "a") as fh:
            fh.write(f"\n## Ingested {now()}\n\n" + "\n".join(f"- {m['name']} -> studies/{pmid}/{m['dest']}"
                                                               for m in moved) + "\n")
    bad = [m["name"] for m in moved if m["dest"] == "sources/paper.pdf" and not m["is_pdf"]]
    print(f"{pmid}: moved {len(moved)} file(s)" + (f"; WARNING not a PDF: {bad}" if bad else "")
          + (f"; {note}" if note else "") + ("" if paper is not None else "; still waiting for paper.pdf"))
    return True


def ingest(pmids: list[str]) -> None:
    if not pmids and INBOX.exists():
        pmids = sorted(p.name for p in INBOX.iterdir() if p.is_dir())
    n = sum(ingest_one(p) for p in pmids)
    print(f"ingested {n} folder(s)")
    write_inbox_index()


def write_inbox_index() -> None:
    INBOX.mkdir(exist_ok=True)
    rows = []
    for d in sorted(p for p in INBOX.iterdir() if p.is_dir()):
        res = load_result(d.name) or {}
        is_open = (d / "WANTED.md").exists()
        if is_open and res.get("task"):
            (d / "WANTED.md").write_text(wanted_text(res))   # follows the latest resolve
        n = len(inbox_files(d))
        state = (f"{n} file(s) dropped, run ingest" if n else "waiting") if is_open else \
            "done" if (d / "DONE.md").exists() else "?"
        task = res.get("task") or {}
        link = next((x["url"] for x in res.get("links_for_human", [])), "")
        rows.append((not is_open, task.get("priority", 9), d.name,
                     f"| {d.name} | {task.get('priority', '')} | {state} | {res.get('title', '')[:70]} | {link} |"))
    rows.sort()
    body = "\n".join(r[3] for r in rows) or "| | | nothing open | | |"
    (INBOX / "README.md").write_text(f"""# Inbox: papers that need a person

Each folder is one paper the resolver could not get legally on its own. Open `<PMID>/WANTED.md`, get
the article and its supplementary files, and put them in that folder: the article as `paper.pdf`,
supplements with their original names. Then run

    python3 -I pipeline/bin/fetch_paper.py ingest

which moves the files into `studies/<PMID>/sources/`, records their sha256 in the study log, and
renames WANTED.md to DONE.md. This folder is git-ignored: paper text never enters the repository.

| PMID | Priority | State | Title | Link |
|---|---|---|---|---|
{body}

_Written by fetch_paper.py at {now()}._
""")


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", help="look for a legal free copy and download it")
    r.add_argument("pmids", nargs="*")
    r.add_argument("--from-tsv", type=Path, help="papers_fulltext.tsv from paper-discover2sra")
    r.add_argument("--status", default="NO_PMC,NOT_OA", help="download_status values to take from the TSV")
    r.add_argument("--force", action="store_true", help="resolve again even if fetched or wont_do")
    t = sub.add_parser("open-task", help="ask a person for the paper (inbox/<PMID>/WANTED.md)")
    t.add_argument("pmid")
    t.add_argument("--priority", type=int, choices=(1, 2, 3), required=True)
    t.add_argument("--reason", required=True, help="why the paper is worth a person's time")
    w = sub.add_parser("wont-do", help="record that the paper is not worth fetching")
    w.add_argument("pmid")
    w.add_argument("--reason", required=True)
    g = sub.add_parser("ingest", help="move files the owner put in inbox/<PMID>/ into studies/<PMID>/sources/")
    g.add_argument("pmids", nargs="*")
    sub.add_parser("inbox", help="rewrite inbox/README.md")
    a = ap.parse_args(argv)

    if a.cmd == "resolve":
        todo = [(p, "") for p in a.pmids]
        if a.from_tsv:
            todo += pmids_from_tsv(a.from_tsv, {s for s in a.status.split(",") if s})
        if not todo:
            ap.error("give PMIDs or --from-tsv")
        for pmid, doi in todo:
            resolve_one(pmid, doi, a.force)
        if INBOX.exists():
            write_inbox_index()
    elif a.cmd == "open-task":
        open_task(a.pmid, a.priority, a.reason)
    elif a.cmd == "wont-do":
        wont_do(a.pmid, a.reason)
    elif a.cmd == "ingest":
        ingest(a.pmids)
    elif a.cmd == "inbox":
        write_inbox_index()


if __name__ == "__main__":
    main()
