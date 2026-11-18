#!/usr/bin/env python3
"""
Load one curated study folder (studies/<PMID>/) into the database.

Idempotent: everything belonging to the study is deleted and reloaded in one transaction.

Usage:
  uv run --with 'psycopg[binary]' python db/load_study.py studies/35756051 [studies/... ...]

Reads from the study folder: sample_sheet.tsv, sra_runs.tsv, validation.json, log.jsonl,
sources/fulltext.xml or sources/paper.pdf, sources/supplements/*, sources/haiku_extraction.json,
fetch_result.json (how the full text was obtained, from pipeline/bin/fetch_paper.py); plus paper
metadata from the first paper-discover2sra/runs/*/papers.tsv that lists the PMID.

Curator knowledge that is not in the files (which control groups are relatives, how
subject ids were derived, open questions for people) sits in STUDY_NOTES below, one
entry per study, so it is explicit and reviewable.
"""
import collections
import csv
import hashlib
import json
import mimetypes
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate import database_url  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "paper-discover2sra/runs"
LOADER = "script:db/load_study.py"

STUDY_NOTES = {
    "35756051": {
        "design": "case_control",
        "subject_key_source": "archive",
        "groups": {
            "HC": {"definition": "Healthy controls"},
            "UCN": {"definition": "UC with a negative C. difficile test", "extra": {"c_difficile_infection": "no"}},
            "UCP": {"definition": "UC with C. difficile infection, sampled at CDI diagnosis", "extra": {"c_difficile_infection": "yes"}},
        },
        "tasks": [],
    },
    "28174747": {
        "design": "case_control",
        "subject_key_source": "archive",
        "family_based": True,
        "family_field": "family_id_archive",
        "relationship_field": "family_role",
        "control_definition": "Healthy first-degree relatives (siblings and parents) of pediatric IBD probands",
        "groups": {
            "Normal": {"definition": "Healthy first-degree relatives of IBD patients", "control_type": "family_member"},
            "CD": {"definition": "Crohn's disease (probands and affected relatives)"},
            "UC": {"definition": "Ulcerative colitis (probands and affected relatives)"},
        },
        "tasks": [],
    },
    "29335008": {
        "design": "case_control",
        "subject_key_source": "archive",
        "groups": {
            "Control": {"definition": "Children with normal colonoscopy (normal colon control)"},
            "Crohn's disease; Responder": {"definition": "Pediatric CD, responded to induction treatment", "extra": {"treatment_response": "responder"}},
            "Crohn's disease; Non-responder": {"definition": "Pediatric CD, needed second induction within 150 days", "extra": {"treatment_response": "non_responder"}},
        },
        "tasks": [
            {"kind": "review_transcription", "title": "Spot-check transcribed Tables S1 and S2 (29335008)",
             "what": "Tables S1 and S2 exist only as images in MOESM1.pdf and were transcribed by an agent; Paris and maintenance columns have no second source.",
             "why": "Image tables cannot be checked mechanically.", "action": "Compare sources/extracted/MOESM1_TableS*_transcribed.tsv with the PDF.", "priority": 3},
        ],
    },
    "37661264": {
        "design": "case_control",
        "subject_key_source": "one_per_sample",
        "subject_key_rule": "52 stool runs = 52 stool participants; biopsies cannot be linked",
        "groups": {
            "CD_F": {"definition": "Crohn's disease, feces"},
            "CD_M": {"definition": "Crohn's disease, colonic mucosa (inflamed 22 + uninflamed 23; not assignable per run)"},
            "HC_F": {"definition": "Healthy controls, feces"},
        },
        "tasks": [
            {"kind": "contact_authors", "title": "Inflamed vs uninflamed key for 45 biopsies (37661264)",
             "what": "Which of the 45 mucosal runs are inflamed (22) and uninflamed (23), which pairs come from one patient, and what the SC/ID/IL/JE prefixes mean.",
             "why": "Not in the paper, supplements, figures or archive.", "action": "Email the corresponding author.", "priority": 2},
        ],
    },
    "23013615": {
        "design": "case_control",
        "subject_key_source": "inferred_rule",
        "subject_key_rule": "samples with A/B/C suffixes (e.g. 7646A, 7646B) are one person",
        "external_files": [("sources/archive/zenodo_1146764_ibd_huttenhower_results.tar.gz",
                            "https://zenodo.org/records/1146764", "third_party_mirror")],
        "sex_conflict": True,
        "groups": {
            "CD": {"definition": "Crohn's disease"}, "Crohn's Disease": {"definition": "Crohn's disease (shotgun samples)"},
            "UC": {"definition": "Ulcerative colitis"}, "Healthy": {"definition": "Healthy controls"},
            "Indeterminate": {"definition": "Indeterminate colitis (blank diagnosis in the metadata, matched by counts to Table 1)",
                              "basis": "count_match"},
        },
        "tasks": [
            {"kind": "resolve_conflict", "title": "Sex coding in the authors' metadata (23013615)",
             "what": "Metadata code M equals the paper's female count in every group; which reading is right?",
             "why": "Two sources disagree; both readings are stored as conflicting observations.",
             "action": "Ask the authors, or find the original OSCCAR/PRISM file.", "priority": 2},
            {"kind": "review_value_map", "title": "Accept a third-party copy as label source (23013615)",
             "what": "All 228 16S labels come from a Zenodo copy (MicrobiomeHD) of the authors' metadata; the original server is dead.",
             "why": "Provenance decision.", "action": "Accept or reject document origin third_party_mirror for this study.", "priority": 3},
        ],
    },
    "36111238": {
        "design": "case_control",
        "subject_key_source": "archive",
        "subject_key_rule": "BioSample description AUC_n / RUC_n / HC_n, one stool sample per subject",
        "groups": {
            "AUC": {"definition": "Active ulcerative colitis (Mayo score / DAI)", "extra": {"disease_activity": "active"}},
            "RUC": {"definition": "Ulcerative colitis in remission", "extra": {"disease_activity": "remission"}},
            "HC": {"definition": "Healthy controls, not related to the patients"},
        },
        "tasks": [],
    },
    "37696680": {
        "design": "case_control",
        "subject_key_source": "archive",
        "subject_key_rule": "RCU-<n> shared by the 16S and ITS2 BioSamples of one patient; CTRL_<n> for biobank controls",
        "groups": {
            "PSC-UC": {"definition": "Pediatric UC with primary sclerosing cholangitis", "extra": {"psc": "true"}},
            "UC": {"definition": "Pediatric ulcerative colitis without PSC"},
            "CTRL": {"definition": "Healthy children of the OPBG reference biobank (PRJNA280490); the paper used an unnamed 26 of them"},
        },
        "tasks": [
            {"kind": "contact_authors", "title": "Which 26 biobank controls were used (37696680)",
             "what": "The paper compares patients with 26 controls taken from 95 pediatric CTRL runs in PRJNA280490 without naming them.",
             "why": "Owner decision 2026-11-17: only the 53 patient runs are processed until the controls are known.",
             "action": "Email the corresponding author for the CTRL run list.", "priority": 3},
        ],
    },
    "42349924": {
        "design": "case_control",
        "subject_key_source": "archive",
        "subject_key_rule": "BioSample subject code S<nnnn>, one stool sample per subject",
        "groups": {
            "CD_pediatric": {"definition": "Newly diagnosed pediatric Crohn's disease (MiSeq V3-V4)"},
            "control_pediatric": {"definition": "Healthy children below 16 years (PRJNA1308018, MiSeq V3-V4)"},
            "CD_adult": {"definition": "Newly diagnosed adult Crohn's disease (PacBio full-length, not processed yet)"},
            "control_adult": {"definition": "Healthy adults (PacBio full-length, not processed yet)"},
        },
        "tasks": [
            {"kind": "contact_authors", "title": "7 controls missing from the archive (42349924)",
             "what": "The paper has 30 pediatric and 82 adult controls; the archive holds 25 and 80.",
             "why": "Neither BioProject has them.", "action": "Ask the authors whether they can be deposited.", "priority": 3},
        ],
    },
}

DIAG = {"cd": "cd", "uc": "uc", "ibdu": "ibdu", "ibd": "ibd", "control": "healthy"}
SKIP_OTHER_KEYS = {"sources", "gender_code_metadata"}
RUN_COLS = {  # canonical -> candidate column names in the heterogeneous sra_runs.tsv files
    "run": ["run_accession", "Run", "sra:Run"],
    "experiment": ["experiment", "experiment_accession", "Experiment", "sra:Experiment"],
    "spots": ["spots", "read_count", "sra:spots", "ena_read_count"],
    "bases": ["bases", "base_count", "sra:bases"],
    "avg_length": ["avg_length", "avgLength", "sra:avgLength"],
    "platform": ["platform", "Platform", "instrument_platform", "sra:Platform"],
    "instrument": ["instrument_model", "Model", "sra:Model"],
    "strategy": ["library_strategy", "LibraryStrategy", "sra:LibraryStrategy"],
    "source": ["library_source", "LibrarySource", "sra:LibrarySource"],
    "selection": ["library_selection", "LibrarySelection", "sra:LibrarySelection"],
    "layout": ["library_layout", "LibraryLayout", "sra:LibraryLayout"],
    "release": ["release_date", "ReleaseDate", "first_public", "sra:ReleaseDate"],
    "fastq": ["ena_fastq_ftp", "fastq_ftp", "ena:fastq_ftp"],
    "md5": ["fastq_md5", "ena:fastq_md5"],
    "fastq_bytes": ["fastq_bytes", "ena:fastq_bytes"],
    "bioproject": ["bioproject", "BioProject", "study_accession", "sra:BioProject"],
}


def pick(row, key):
    for c in RUN_COLS[key]:
        if row.get(c) not in (None, ""):
            return row[c]
    return None


def num(v):
    try:
        f = float(str(v).replace(",", ""))
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def source_type(src):
    s = (src or "").lower()
    if not s:
        return "derived"
    if "experiment_title" in s or s.startswith("sra:exp"):
        return "archive_title"
    if "sample_name" in s:
        return "archive_name_pattern"
    if s.startswith(("biosample", "ena", "sra")):
        return "archive_attribute"
    if s.startswith("supplement"):
        return "supplement"
    if s.startswith("paper"):
        return "paper_table" if "table" in s else "paper_text"
    if s.startswith("external"):
        return "external_file"
    return "derived"


def paper_meta(pmid, runs_dir=RUNS_DIR):
    """The papers.tsv row for this PMID from the first discovery run (sorted by name) that has it."""
    for tsv in sorted(Path(runs_dir).glob("*/papers.tsv")):
        with open(tsv, encoding="utf-8") as f:
            row = next((r for r in csv.DictReader(f, delimiter="\t") if r["pmid"] == pmid), None)
        if row:
            return row
    return {}


PDF_ORIGIN = {"publisher": "publisher", "repository": "data_repository", "europepmc": "europepmc"}


def fulltext_sources(folder):
    """How the study's full text was obtained, without touching the database.

    Returns (fulltext_status for core.paper, documents), where documents is a list of
    (relative path, kind, origin, obtained_by) for the files that exist. fetch_result.json is
    written by pipeline/bin/fetch_paper.py; studies curated before it existed have only
    sources/fulltext.xml from the PMC open-access subset.
    """
    folder = Path(folder)
    fr_path = folder / "fetch_result.json"
    fr = json.loads(fr_path.read_text(encoding="utf-8")) if fr_path.exists() else {}
    human = {h.get("dest") for h in fr.get("human_files") or []}
    has_xml = (folder / "sources/fulltext.xml").exists()
    has_pdf = (folder / "sources/paper.pdf").exists()
    status = {"fetched": "fetched_oa", "fetched_by_human": "human_supplied", "needs_human": "not_oa",
              "wont_do": "unavailable"}.get(fr.get("status"))
    if status is None:
        status = "fetched_oa" if has_xml else "not_tried"

    docs = []
    if has_xml:
        docs.append(("sources/fulltext.xml", "fulltext_xml", "pmc", "agent"))
    if has_pdf:
        if "sources/paper.pdf" in human or fr.get("status") == "fetched_by_human":
            docs.append(("sources/paper.pdf", "fulltext_pdf", "human_upload", "human"))
        else:
            route = (fr.get("route") or "").split(":")[-1].lower()
            docs.append(("sources/paper.pdf", "fulltext_pdf", PDF_ORIGIN.get(route, "publisher"), "script"))
    supp = folder / "sources/supplements"
    for f in sorted(supp.glob("*")) if supp.exists() else []:
        if f.is_file():
            rel = str(f.relative_to(folder))
            docs.append((rel, "supplement", "human_upload", "human") if rel in human
                        else (rel, "supplement", "europepmc", "agent"))
    return status, docs


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def https(url):
    return url if "://" in url else "https://" + url


class Loader:
    def __init__(self, conn, folder):
        self.c = conn
        self.dir = Path(folder).resolve()
        self.pmid = self.dir.name
        self.notes = STUDY_NOTES.get(self.pmid, {})
        self.sheet = list(csv.DictReader(open(self.dir / "sample_sheet.tsv", encoding="utf-8"), delimiter="\t"))
        self.runs_raw = list(csv.DictReader(open(self.dir / "sra_runs.tsv", encoding="utf-8"), delimiter="\t"))
        self.validation = json.load(open(self.dir / "validation.json"))
        self.counts = collections.Counter()

    def q(self, sql, params=None):
        return self.c.execute(sql, params)

    def one(self, sql, params=None):
        return self.c.execute(sql, params).fetchone()[0]

    # ------------------------------------------------------------ reset
    def reset(self):
        sid = self.c.execute("select study_id from core.study where study_key like %s", (f"%-{self.pmid}",)).fetchone()
        projects = [r[0] for r in self.q("select project_accession from core.archive_project where study_id = %s",
                                         (sid[0] if sid else -1,))]
        if sid:
            self.q("delete from core.study where study_id = %s", (sid[0],))
        if projects:
            self.q("delete from core.archive_project where project_accession = any(%s)", (projects,))
        self.q("delete from raw.api_record where accession = %s", (f"study:{self.pmid}",))

    # ------------------------------------------------------------ paper, study
    def paper_and_study(self):
        meta = paper_meta(self.pmid)
        self.fulltext_status, self.fulltext_docs = fulltext_sources(self.dir)
        self.paper_id = self.one("""
            insert into core.paper (pmid, pmcid, doi, title, journal, pub_year, is_open_access, abstract, fulltext_status)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (pmid) do update set title = excluded.title, fulltext_status = excluded.fulltext_status
            returning paper_id""",
            (self.pmid, meta.get("pmc_id") or None, (meta.get("doi") or "").lower() or None,
             meta.get("title") or f"PMID {self.pmid}", meta.get("journal"), num(meta.get("pub_date")),
             meta.get("is_open_access") == "Y", meta.get("abstract"), self.fulltext_status))
        v = self.validation
        year = meta.get("pub_date") or "na"
        self.study_key = f"ibd-{year}-{self.pmid}"
        self.study_id = self.one("""
            insert into core.study (study_key, primary_paper_id, disease_id, title, design, screening_decision,
              curation_state, family_based, control_definition, verdict, verdict_reason, notes)
            values (%s,%s,(select disease_id from core.disease where code='ibd'),%s,%s,'include',%s,%s,%s,%s,%s,%s)
            returning study_id""",
            (self.study_key, self.paper_id, meta.get("title"), self.notes.get("design"),
             "labels_validated" if v.get("verdict") == "complete" else "labels_located",
             self.notes.get("family_based"), self.notes.get("control_definition"),
             v.get("verdict"), v.get("verdict_reason"), f"Curated in studies/{self.pmid}/; see summary.md"))
        self.q("insert into core.study_paper (study_id, paper_id, role) values (%s,%s,'primary')", (self.study_id, self.paper_id))

    # ------------------------------------------------------------ log
    def activity_log(self):
        path = self.dir / "log.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            outcome = e.get("outcome") if e.get("outcome") in ("success", "failure", "blocked", "skipped", "partial") else "partial"
            self.q("""insert into ops.activity_log (at, study_id, actor_type, actor, session_id, step, action, input, outcome, detail)
                      values (%s,%s,'agent',%s,%s,%s,%s,%s,%s,%s)""",
                   (e.get("ts"), self.study_id, e.get("actor", "agent"), f"study-curation-{self.pmid}", e.get("step"),
                    (e.get("action") or "")[:500], Jsonb({"source": e.get("source")}), outcome,
                    Jsonb({"detail": e.get("detail"), "files": e.get("files")})))
            self.counts["activity_log"] += 1
        self.log_id = self.one("""insert into ops.activity_log (study_id, actor_type, actor, step, action, outcome, detail)
                                  values (%s,'script',%s,'load','load curated study into database','success',%s) returning log_id""",
                               (self.study_id, LOADER, Jsonb({"folder": str(self.dir)})))

    # ------------------------------------------------------------ documents
    def add_document(self, path, kind, origin, url=None, obtained_by="agent"):
        p = self.dir / path if not Path(path).is_absolute() else Path(path)
        if not p.exists():
            return None
        doc = self.one("""insert into core.document (paper_id, study_id, kind, original_name, source_url, origin, obtained_by,
                            storage_path, sha256, bytes, mime_type, log_id)
                          values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                          on conflict (sha256) do update set study_id = excluded.study_id returning document_id""",
                       (self.paper_id, self.study_id, kind, p.name, url, origin, obtained_by, str(p.relative_to(ROOT)), sha256(p),
                        p.stat().st_size, mimetypes.guess_type(p.name)[0], self.log_id))
        self.counts["document"] += 1
        return doc

    def documents(self):
        for path, kind, origin, obtained_by in self.fulltext_docs:
            self.add_document(path, kind, origin, obtained_by=obtained_by)
        self.external_doc = None
        for path, url, origin in self.notes.get("external_files", []):
            self.external_doc = self.add_document(path, "external_metadata", origin, url)

    # ------------------------------------------------------------ raw archive table
    def raw_archive(self):
        text = (self.dir / "sra_runs.tsv").read_text(encoding="utf-8")
        rec = self.one("""insert into raw.api_record (source, accession, request, content_type, payload_text, payload_sha256)
                          values ('curated_table', %s, %s, 'tsv', %s, %s) returning record_id""",
                       (f"study:{self.pmid}", f"studies/{self.pmid}/sra_runs.tsv", text, hashlib.sha256(text.encode()).hexdigest()))
        self.raw_record_id = rec
        rows = []
        for r in self.runs_raw:
            acc = pick(r, "run")
            proj = pick(r, "bioproject")
            for k, v in r.items():
                if k and v not in (None, ""):
                    rows.append((acc, "run", k, rec, v, proj))
        with self.c.cursor() as cur:
            cur.executemany("""insert into raw.archive_attribute (accession, entity_type, field_name, record_id, value, project_accession)
                               values (%s,%s,%s,%s,%s,%s) on conflict do nothing""", rows)
        self.counts["archive_attribute"] += len(rows)
        self.raw_by_run = {pick(r, "run"): r for r in self.runs_raw}

    # ------------------------------------------------------------ projects, protocol, extraction
    def projects_and_protocol(self):
        per_proj = collections.Counter(r["bioproject"] for r in self.sheet)
        for proj, n in per_proj.items():
            archive = "ena" if proj.startswith("PRJEB") else "ddbj" if proj.startswith("PRJDB") else "ncbi"
            self.q("""insert into core.archive_project (project_accession, study_id, archive, n_runs_archive, raw_record_id)
                      values (%s,%s,%s,%s,%s)""", (proj, self.study_id, archive, n, self.raw_record_id))
        haiku = {}
        hp = self.dir / "sources/haiku_extraction.json"
        if hp.exists():
            meta = json.load(open(hp))
            txt = meta.get("text", "")
            try:
                haiku = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
            except json.JSONDecodeError:
                haiku = {}
        r16 = [self.raw_by_run.get(r["run_accession"], {}) for r in self.sheet if r["is_16s"] == "yes"]
        inst = collections.Counter(pick(r, "instrument") for r in r16 if pick(r, "instrument")).most_common(1)
        lay = collections.Counter((pick(r, "layout") or "").upper() for r in r16 if pick(r, "layout")).most_common(1)
        plat = collections.Counter((pick(r, "platform") or "").upper() for r in r16 if pick(r, "platform")).most_common(1)
        self.protocol_id = self.one("""
            insert into core.protocol (study_id, label, variable_region, platform, instrument_model, layout, extra)
            values (%s,'main',%s,%s,%s,%s,%s) returning protocol_id""",
            (self.study_id, haiku.get("variable_region"), plat[0][0] if plat else None, inst[0][0] if inst else None,
             lay[0][0] if lay and lay[0][0] in ("PAIRED", "SINGLE") else None,
             Jsonb({"variable_region_source": "Haiku extraction (unverified)"})))
        for field in ("variable_region", "sequencing_platform", "sample_size", "n_cases", "n_controls", "sample_type"):
            if haiku.get(field) not in (None, "", []):
                self.q("""insert into core.extraction (study_id, protocol_id, field, value, extractor, schema_version, status)
                          values (%s,%s,%s,%s,%s,'paper-discover2sra-extract-v1','proposed')""",
                       (self.study_id, self.protocol_id, field, str(haiku[field]),
                        ",".join(json.load(open(hp)).get("models_used") or ["haiku"])))
                self.counts["extraction"] += 1

    # ------------------------------------------------------------ groups and value maps
    def groups(self):
        self.group_ids, self.value_maps = {}, {}
        by_label = collections.defaultdict(list)
        for r in self.sheet:
            if r["group_paper"]:
                by_label[r["group_paper"]].append(r)
        gnotes = self.notes.get("groups", {})
        for label, rows in by_label.items():
            role = collections.Counter(r["group"] for r in rows).most_common(1)[0][0] or None
            role = role if role in ("case", "control", "comparator", "other") else None
            dis = collections.Counter(r["disease"].lower() for r in rows).most_common(1)[0][0]
            dcode = DIAG.get(dis)
            gn = gnotes.get(label, {})
            gid = self.one("""insert into core.study_group (study_id, label, definition, diagnosis_code, role, control_type, extra)
                              values (%s,%s,%s,%s,%s,%s,%s) returning group_id""",
                           (self.study_id, label, gn.get("definition"), dcode if dcode != "healthy" else None, role,
                            gn.get("control_type"), Jsonb(gn.get("extra", {}))))
            self.group_ids[label] = gid
            src = collections.Counter(r["source_group"] for r in rows).most_common(1)[0][0]
            basis = gn.get("basis", "stated")
            self.value_maps[label] = self.one("""
                insert into core.value_map (study_id, source_field, raw_value, field, value_norm, also_sets, evidence, basis, status, log_id)
                values (%s,%s,%s,'diagnosis',%s,%s,%s,%s,'accepted',%s) returning value_map_id""",
                (self.study_id, src or "sample sheet group_paper", label, dcode, Jsonb(gn.get("extra", {})),
                 gn.get("definition"), basis, self.log_id))
            self.counts["study_group"] += 1

    # ------------------------------------------------------------ subjects, samples, runs, observations
    def entities(self):
        self.obs = []  # (entity_type, entity_id, field, raw, norm, num, unit, source, locator, value_map_id, confidence, note, forced_status, conflict_group)
        subj_ids = {}
        subj_source = self.notes.get("subject_key_source", "archive")
        fam_field = self.notes.get("family_field")
        rel_field = self.notes.get("relationship_field")

        # one sample per BioSample (several runs can share it)
        by_sample = collections.OrderedDict()
        for r in self.sheet:
            by_sample.setdefault(r["biosample"] or r["sample_name_archive"] or r["run_accession"], []).append(r)

        for skey, rows in by_sample.items():
            r = rows[0]
            other = json.loads(r["other_json"] or "{}")
            gid = self.group_ids.get(r["group_paper"])
            subject_id = None
            if r["subject_id"]:
                sk = r["subject_id"]
                if sk not in subj_ids:
                    subj_ids[sk] = self.one("""
                        insert into core.subject (study_id, subject_key, subject_key_source, subject_key_rule, family_key, group_id)
                        values (%s,%s,%s,%s,%s,%s) returning subject_id""",
                        (self.study_id, sk, subj_source, self.notes.get("subject_key_rule"),
                         str(other.get(fam_field)) if fam_field and other.get(fam_field) else None, gid))
                    self.counts["subject"] += 1
                subject_id = subj_ids[sk]
            pool = r["group"] == "other" and "unassigned barcodes" in r["notes"].lower()
            role = "technical_pool" if pool else "biological"
            used = other.get("used_in_paper_16s_analysis")
            used = False if str(used) == "False" or pool else (True if (str(used) == "True" or r["group_paper"]) else None)
            link = ("not_applicable" if pool else "unknown") if subject_id is None else \
                   {"archive": "known", "paper": "known", "inferred_rule": "inferred", "one_per_sample": "one_per_sample"}[subj_source]
            sample_id = self.one("""
                insert into core.sample (study_id, subject_id, subject_link, group_id, used_in_paper, biosample_accession,
                  archive_sample_name, paper_sample_name, sample_role, label_source, raw_record_id)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning sample_id""",
                (self.study_id, subject_id, link, gid, used, r["biosample"] or None, r["sample_name_archive"] or None,
                 r["sample_name_paper"] or None, role, source_type(r["source_group"]) if gid else None, self.raw_record_id))
            self.counts["sample"] += 1

            for rr in rows:
                raw = self.raw_by_run.get(rr["run_accession"], {})
                assay = "16s_amplicon" if rr["is_16s"] == "yes" else \
                        "shotgun" if (rr["library_strategy"] or "").upper() in ("WGS", "WXS", "WGA", "METAGENOMIC") else "unknown"
                state = "skipped_not_16s" if assay != "16s_amplicon" else \
                        "skipped_not_used" if (role != "biological" or used is False) else "queued"
                urls = [https(u) for u in (pick(raw, "fastq") or "").split(";") if u]
                md5 = [m for m in (pick(raw, "md5") or "").split(";") if m]
                fbytes = [int(b) for b in (pick(raw, "fastq_bytes") or "").split(";") if b.strip().isdigit()]
                self.q("""insert into core.run (run_accession, sample_id, experiment_accession, project_accession, protocol_id,
                            platform, instrument_model, library_strategy, library_source, library_selection, library_layout,
                            assay, used_in_paper, spots, bases, avg_length_archive, release_date, fastq_urls, fastq_md5, fastq_bytes,
                            processing_state, raw_record_id)
                          values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                       (rr["run_accession"], sample_id, pick(raw, "experiment"), rr["bioproject"],
                        self.protocol_id if assay == "16s_amplicon" else None,
                        (pick(raw, "platform") or "").upper() or None, pick(raw, "instrument"),
                        rr["library_strategy"] or pick(raw, "strategy"), pick(raw, "source"), pick(raw, "selection"),
                        (pick(raw, "layout") or "").upper() or None, assay, used,
                        num(pick(raw, "spots")), num(pick(raw, "bases")), num(pick(raw, "avg_length")),
                        (pick(raw, "release") or None) and pick(raw, "release")[:19],
                        urls or None, md5 or None, fbytes or None, state, self.raw_record_id))
                self.counts["run"] += 1

            # ---- observations
            conf = r["confidence"] or None
            subj_or_sample = ("subject", subject_id) if subject_id else ("sample", sample_id)
            dis = DIAG.get(r["disease"].lower(), ("other" if r["disease"] else None))
            if dis:
                self.obs.append((*subj_or_sample, "diagnosis", r["disease"], dis, None, None, r["source_group"],
                                 {"column": "disease"}, self.value_maps.get(r["group_paper"]), conf, None))
            if r["group_paper"]:
                self.obs.append(("sample", sample_id, "group_paper", r["group_paper"], r["group_paper"], None, None,
                                 r["source_group"], {"column": "group_paper"}, self.value_maps.get(r["group_paper"]), conf, None))
            if r["sex"]:
                self.obs.append((*subj_or_sample, "sex", r["sex"], r["sex"], None, None, r["source_sex"], {"column": "sex"}, None, conf, None))
            if r["age_years"]:  # age is per sample (age at sampling)
                self.obs.append(("sample", sample_id, "age_years", r["age_raw"] or r["age_years"], r["age_years"], num(r["age_years"]),
                                 "years", r["source_age"], {"column": "age_years"}, None, conf, None))
            for col, field in (("sample_type", "sample_type"), ("body_site", "body_site"), ("timepoint", "timepoint"),
                               ("treatment", "treatment")):
                if r[col]:
                    self.obs.append(("sample", sample_id, field, r[col], r[col], None, None, "derived:sample_sheet",
                                     {"column": col}, None, conf, None))
            if r["inflamed"]:
                self.obs.append(("sample", sample_id, "inflamed", r["inflamed"], {"yes": "true", "no": "false"}.get(r["inflamed"]),
                                 None, None, "derived:sample_sheet", {"column": "inflamed"}, None, conf, None))
            gnote = self.notes.get("groups", {}).get(r["group_paper"], {})
            if gnote.get("control_type") and subject_id:
                self.obs.append(("subject", subject_id, "control_type", gnote["control_type"], gnote["control_type"], None, None,
                                 r["source_group"], {"group": r["group_paper"]}, self.value_maps.get(r["group_paper"]), conf, None))
            for k, v in (gnote.get("extra") or {}).items():
                self.obs.append(("sample", sample_id, k, r["group_paper"], v, None, None, r["source_group"],
                                 {"implied_by_group": r["group_paper"]}, self.value_maps.get(r["group_paper"]), conf, None))
            if rel_field and other.get(rel_field) and subject_id:
                rv = other[rel_field]
                norm = "proband" if rv == "Proband" else "sibling" if rv.startswith("Sibling") else \
                       "parent" if rv in ("Mother", "Father") else "other"
                self.obs.append(("subject", subject_id, "relationship_to_proband", rv, norm, None, None,
                                 "biosample:host_family_relationship", {"attribute": "host_family_relationship"}, None, conf, None))
            if fam_field and other.get(fam_field) and subject_id:
                self.obs.append(("subject", subject_id, "family_key", str(other[fam_field]), str(other[fam_field]), None, None,
                                 "biosample:host_subject_id", {"other_json": fam_field}, None, conf, None))
            for k, v in other.items():
                if k in SKIP_OTHER_KEYS or k in (fam_field, rel_field) or v in (None, "", [], {}):
                    continue
                if k in (gnote.get("extra") or {}):
                    continue
                val = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                self.obs.append(("sample", sample_id, k, val, val, num(v) if not isinstance(v, bool) else None, None,
                                 "derived:sample_sheet", {"other_json": k}, None, conf, None))
            if self.notes.get("sex_conflict") and other.get("gender_code_metadata") and subject_id:
                code = other["gender_code_metadata"]
                literal = {"M": "male", "F": "female"}.get(code)
                inverted = {"M": "female", "F": "male"}.get(code)
                self.obs.append(("subject", subject_id, "sex", code, literal, None, None, "external:metadata gender",
                                 {"file": "ibd_huttenhower.metadata.txt", "column": "gender"}, None, "medium",
                                 "literal reading of the authors' code", "conflict"))
                self.obs.append(("subject", subject_id, "sex", code, inverted, None, None, "inferred:paper Table 1 counts",
                                 {"table": "Table 1"}, None, "medium",
                                 "metadata M count equals the paper's female count in every group", "conflict"))

    def write_observations(self):
        fields = {r[0] for r in self.q("select field from core.field_dictionary")}
        # de-duplicate (same entity, field, value) and detect conflicts between different values
        keyed = collections.OrderedDict()
        for o in self.obs:
            et, eid, field, norm = o[0], o[1], o[2], o[4]
            k = (et, eid, field, str(norm), o[7])
            if k not in keyed:
                keyed[k] = o
        by_ef = collections.defaultdict(list)
        for k, o in keyed.items():
            by_ef[(o[0], o[1], o[2])].append(o)
        conflict_seq = self.one("select coalesce(max(conflict_group), 0) from core.observation")
        rows = []
        for (et, eid, field), obs in by_ef.items():
            values = {str(o[4]) for o in obs}
            forced = any(len(o) > 12 and o[12] == "conflict" for o in obs)
            conflict = forced or len(values) > 1
            cg = None
            if conflict:
                conflict_seq += 1
                cg = conflict_seq
            for i, o in enumerate(obs):
                # same value from several sources: the first is accepted, the others corroborate it
                status = "conflict" if conflict else ("accepted" if i == 0 else "candidate")
                src = o[7] or ""
                rows.append((self.study_id, et,
                             eid if et == "subject" else None, eid if et == "sample" else None, None,
                             field, field in fields, o[3], o[4], o[5], o[6], source_type(src),
                             self.external_doc if source_type(src) == "external_file" else None,
                             Jsonb({**(o[8] or {}), "source": src}), o[9], status, cg, o[10],
                             o[11] if len(o) > 11 else None, self.log_id))
        with self.c.cursor() as cur:
            cur.executemany("""insert into core.observation (study_id, entity_type, subject_id, sample_id, run_accession, field,
                                 registered, value_raw, value_norm, value_num, unit, source_type, source_document_id, source_locator,
                                 value_map_id, status, conflict_group, confidence, note, log_id)
                               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        self.counts["observation"] += len(rows)
        self.counts["observation_conflict"] += sum(1 for r in rows if r[15] == "conflict")

    def rebuild_typed(self):
        sid = self.study_id
        # subject typed columns from accepted observations
        self.q("""update core.sample s set age_at_sampling = o.value_num from core.observation o
                  where o.sample_id = s.sample_id and o.field = 'age_years' and o.status = 'accepted' and s.study_id = %s""", (sid,))
        self.q("""update core.subject j set age_years = a.age from (
                    select subject_id, min(age_at_sampling) as age from core.sample
                    where study_id = %s and age_at_sampling is not null group by subject_id) a
                  where a.subject_id = j.subject_id""", (sid,))
        for field, col, cast in (("sex", "sex", "text"),
                                 ("diagnosis", "diagnosis", "text"), ("control_type", "control_type", "text")):
            expr = "o.value_num" if cast == "numeric" else "o.value_norm"
            self.q(f"""update core.subject s set {col} = {expr} from core.observation o
                       where o.subject_id = s.subject_id and o.field = %s and o.status = 'accepted' and s.study_id = %s""", (field, sid))
        self.q("""update core.subject s set extra = coalesce((
                    select jsonb_object_agg(o.field, o.value_norm) from core.observation o
                    left join core.field_dictionary d on d.field = o.field
                    where o.subject_id = s.subject_id and o.status = 'accepted' and d.promoted_to is null), '{}')
                  where s.study_id = %s""", (sid,))
        for field, col, expr in (("sample_type", "sample_type", "o.value_norm"), ("body_site", "body_site", "o.value_norm"),
                                 ("timepoint", "timepoint", "o.value_norm"), ("inflamed", "inflamed", "o.value_norm::boolean")):
            self.q(f"""update core.sample s set {col} = {expr} from core.observation o
                       where o.sample_id = s.sample_id and o.field = %s and o.status = 'accepted' and s.study_id = %s""", (field, sid))
        self.q("""update core.sample s set extra = coalesce((
                    select jsonb_object_agg(o.field, o.value_norm) from core.observation o
                    left join core.field_dictionary d on d.field = o.field
                    where o.sample_id = s.sample_id and o.status = 'accepted'
                      and (d.promoted_to is null or (o.field = 'diagnosis'))), '{}')
                  where s.study_id = %s""", (sid,))
        self.q("""update core.sample s set n_open_conflicts = (
                    select count(distinct o.field) from core.observation o
                    where o.status = 'conflict' and (o.sample_id = s.sample_id or o.subject_id = s.subject_id))
                  where s.study_id = %s""", (sid,))

    # ------------------------------------------------------------ reported counts and checks
    def checks(self):
        seen = collections.Counter()
        for chk in self.validation.get("checks", []):
            name = chk.get("name") or "check"
            seen[name] += 1
            if seen[name] > 1:
                name = f"{name}_{seen[name]}"
            low = name.lower()
            unit = "runs" if "run" in low else "reads" if "read" in low else "families" if "famil" in low else \
                   "samples" if "sample" in low else "subjects"
            paper, sheet = chk.get("paper"), chk.get("sheet")
            pv, sv = num(paper) if not isinstance(paper, bool) else None, num(sheet) if not isinstance(sheet, bool) else None
            cid = self.one("""insert into core.reported_count (study_id, name, unit, value, value_text, evidence_quote, evidence_location)
                              values (%s,%s,%s,%s,%s,%s,%s) returning count_id""",
                           (self.study_id, name, unit, pv, None if pv is not None else (json.dumps(paper) if paper is not None else None),
                            (chk.get("evidence") or "")[:400] or None, chk.get("where")))
            result = {True: "pass", False: "fail"}.get(chk.get("pass"), "not_comparable")
            self.q("""insert into ops.validation_check (study_id, check_name, count_id, expected, observed, expected_text,
                        observed_text, result, detail, log_id)
                      values (%s,'reported_count',%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (self.study_id, cid, pv, sv, None if pv is not None else str(paper), None if sv is not None else str(sheet),
                    result, Jsonb({k: chk[k] for k in chk if k in ("note", "gap_explained_by_missing_samples")}), self.log_id))
            self.counts["validation_check"] += 1

    def tasks(self):
        for t in self.notes.get("tasks", []):
            self.q("""insert into ops.human_task (study_id, kind, title, what_is_missing, why_blocked, suggested_action, priority,
                        resume_stage, opened_log_id) values (%s,%s,%s,%s,%s,%s,%s,'labels_validated',%s)""",
                   (self.study_id, t["kind"], t["title"], t["what"], t.get("why"), t.get("action"), t.get("priority", 2), self.log_id))
            self.counts["human_task"] += 1

    def run(self):
        with self.c.transaction():
            self.reset()
            self.paper_and_study()
            self.activity_log()
            self.documents()
            self.raw_archive()
            self.projects_and_protocol()
            self.groups()
            self.entities()
            self.write_observations()
            self.rebuild_typed()
            self.checks()
            self.tasks()
        return self.counts


def main():
    folders = sys.argv[1:] or sorted(str(p) for p in (ROOT / "studies").glob("[0-9]*") if p.is_dir())
    with psycopg.connect(database_url()) as conn:
        for f in folders:
            counts = Loader(conn, f).run()
            print(Path(f).name, dict(counts))


if __name__ == "__main__":
    main()
