"""
agentic-16s data browser: a read-only look at the PostgreSQL database.

Run:  uv run --with streamlit --with 'psycopg[binary]' --with pandas streamlit run dashboard/app.py
"""
import os
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
st.set_page_config(page_title="agentic-16s data", page_icon="🧫", layout="wide")


def database_url():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return "postgresql:///agentic16s"


@st.cache_resource
def connect():
    conn = psycopg.connect(database_url(), autocommit=True)
    conn.execute("set default_transaction_read_only = on")
    return conn


@st.cache_data(ttl=60)
def q(sql, params=None):
    with connect().cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def table(df, height=None, **kw):
    if height:
        kw["height"] = height
    st.dataframe(df, width="stretch", hide_index=True, **kw)


# ------------------------------------------------------------------ sidebar
st.sidebar.title("agentic-16s")
st.sidebar.caption("Read-only view of the `agentic16s` database. Data refreshes every minute.")
st.sidebar.caption("Research use only: not a medical device and not for diagnosis or treatment decisions.")
page = st.sidebar.radio("Page", ["Overview", "Studies", "Samples", "Cohorts", "Fields", "Open questions", "Bioinformatics",
                                 "Model: IBD vs healthy"])
if st.sidebar.button("Refresh now"):
    st.cache_data.clear()

studies = q("select study_id, study_key from core.study order by study_key")

# ------------------------------------------------------------------ overview
if page == "Overview":
    st.title("What is in the database")
    tot = q("""select
        (select count(*) from core.study) studies,
        (select count(*) from core.run where assay = '16s_amplicon') runs_16s,
        (select count(*) from core.run r join core.sample s using (sample_id) where r.assay='16s_amplicon' and s.group_id is not null) with_group,
        (select count(*) from core.run r join analysis.sample_wide w using (sample_id) where r.assay='16s_amplicon' and w.sex is not null) with_sex,
        (select count(*) from core.run r join analysis.sample_wide w using (sample_id) where r.assay='16s_amplicon' and w.age_years is not null) with_age,
        (select count(*) from core.observation) observations,
        (select count(*) from ops.human_task where status in ('open','in_progress')) open_tasks""").iloc[0]
    c = st.columns(6)
    c[0].metric("Studies", int(tot.studies))
    c[1].metric("16S runs", int(tot.runs_16s))
    c[2].metric("with group", int(tot.with_group), help="16S runs whose sample has a case/control group")
    c[2].caption(f"{tot.with_group / tot.runs_16s:.0%} of 16S runs")
    c[3].metric("with sex", int(tot.with_sex), help="16S runs with an accepted sex value")
    c[3].caption(f"{tot.with_sex / tot.runs_16s:.0%} of 16S runs")
    c[4].metric("with age", int(tot.with_age), help="16S runs with an age at sampling")
    c[4].caption(f"{tot.with_age / tot.runs_16s:.0%} of 16S runs")
    c[5].metric("Open questions", int(tot.open_tasks))

    st.subheader("Studies")
    table(q("""select study_key as study, verdict, n_samples as samples, n_with_group as "with group",
                      n_runs as runs, n_runs_16s as "16S runs", n_runs_processed as processed,
                      n_checks_pass as "checks passed", n_checks_fail as "checks failed",
                      n_checks_not_comparable as "not comparable", n_open_tasks as "open questions"
               from analysis.study_progress order by study_key"""))

    left, right = st.columns(2)
    with left:
        st.subheader("Samples per group")
        g = q("""select st.study_key as study, coalesce(w.diagnosis, 'unknown') as diagnosis, count(*) n
                 from analysis.sample_wide w join core.study st using (study_id)
                 where w.n_runs_16s > 0 group by 1, 2 order by 1, 2""")
        st.bar_chart(g.pivot(index="study", columns="diagnosis", values="n").fillna(0), horizontal=True)
    with right:
        st.subheader("Where the values came from")
        table(q("""select source_type as source, status, count(*) as values
                   from core.observation group by 1, 2 order by 3 desc"""))

# ------------------------------------------------------------------ studies
elif page == "Studies":
    st.title("Studies")
    key = st.selectbox("Study", studies.study_key)
    sid = int(studies.set_index("study_key").loc[key, "study_id"])
    info = q("""select st.study_key, p.title, p.journal, p.pub_year, p.pmid, p.pmcid, st.verdict, st.verdict_reason,
                       st.family_based, st.control_definition,
                       (select string_agg(project_accession, ', ') from core.archive_project a where a.study_id = st.study_id) as projects
                from core.study st left join core.paper p on p.paper_id = st.primary_paper_id where st.study_id = %s""", (sid,)).iloc[0]
    st.markdown(f"**{info.title}**  \n{info.journal} {info.pub_year} · "
                f"[PubMed {info.pmid}](https://pubmed.ncbi.nlm.nih.gov/{info.pmid}/) · {info.pmcid or ''} · archive: {info.projects}")
    st.info(f"**{info.verdict}**: {info.verdict_reason}")

    t1, t2, t3, t4, t5 = st.tabs(["Groups", "Checks against the paper", "Protocol", "Documents", "Activity log"])
    with t1:
        table(q("""select g.label as "group (paper's words)", g.role, g.diagnosis_code as diagnosis, g.control_type,
                          g.definition, g.extra::text as "implies",
                          (select count(*) from core.sample s where s.group_id = g.group_id) as samples,
                          v.source_field as "label found in", v.basis
                   from core.study_group g left join core.value_map v on v.study_id = g.study_id and v.raw_value = g.label
                   where g.study_id = %s order by samples desc""", (sid,)))
    with t2:
        chk = q("""select c.name as "what the paper states", c.unit, coalesce(c.value::text, c.value_text) as paper,
                          coalesce(v.observed::text, v.observed_text) as database, v.result,
                          c.evidence_location as "where", c.evidence_quote as evidence
                   from core.reported_count c join ops.validation_check v on v.count_id = c.count_id
                   where c.study_id = %s order by v.result desc, c.name""", (sid,))
        f = st.multiselect("Show", ["pass", "fail", "not_comparable"], default=["fail", "not_comparable", "pass"])
        table(chk[chk.result.isin(f)], column_config={"evidence": st.column_config.TextColumn(width="large")})
    with t3:
        table(q("""select label, variable_region, platform, instrument_model, layout, extra::text as notes
                   from core.protocol where study_id = %s""", (sid,)))
        st.caption("LLM extractions (unverified):")
        table(q("select field, value, extractor, status from core.extraction where study_id = %s", (sid,)))
    with t4:
        table(q("""select kind, original_name as file, origin, pg_size_pretty(bytes) as size, storage_path as path
                   from core.document where study_id = %s order by kind""", (sid,)))
    with t5:
        table(q("""select to_char(at, 'YYYY-MM-DD HH24:MI') as time, actor, step, action, outcome,
                          coalesce(detail->>'detail', reason) as detail
                   from ops.activity_log where study_id = %s order by log_id""", (sid,)),
              column_config={"detail": st.column_config.TextColumn(width="large")})

# ------------------------------------------------------------------ samples
elif page == "Samples":
    st.title("Samples")
    c = st.columns(4)
    pick = c[0].multiselect("Study", studies.study_key)
    diag = c[1].multiselect("Diagnosis", ["cd", "uc", "ibdu", "healthy"])
    stype = c[2].multiselect("Sample type", ["stool", "mucosal_biopsy"])
    only16 = c[3].checkbox("Only samples with 16S runs", value=True)
    df = q("""select w.sample_id, w.study_key as study, w.biosample_accession as biosample, w.archive_sample_name as "archive name",
                     w.subject_key as subject, w.family_key as family, w.group_label as "group", w.diagnosis, w.control_type,
                     w.sex, w.age_years as age, w.sample_type, w.body_site, w.inflamed, w.used_in_paper,
                     w.subject_link, w.n_open_conflicts as conflicts, w.n_runs_16s as "16S runs", w.spots_16s as reads,
                     w.variable_region as region, w.platform, w.extra::text as "other fields"
              from analysis.sample_wide w order by w.study_key, w.sample_id""")
    if pick:
        df = df[df.study.isin(pick)]
    if diag:
        df = df[df.diagnosis.isin(diag)]
    if stype:
        df = df[df.sample_type.isin(stype)]
    if only16:
        df = df[df["16S runs"] > 0]
    st.caption(f"{len(df)} samples. Click a row to see every value behind it and where it came from.")
    ev = st.dataframe(df, width="stretch", hide_index=True, height=420, on_select="rerun",
                      selection_mode="single-row", column_config={"sample_id": None})
    rows = ev.selection.rows if ev and ev.selection else []
    if rows:
        sample_id = int(df.iloc[rows[0]].sample_id)
        s = df.iloc[rows[0]]
        st.subheader(f"{s['archive name'] or s.biosample} · {s.study}")
        obs = q("""select o.entity_type as about, o.field, o.value_raw as "as found", o.value_norm as value, o.status,
                          o.source_type as source, o.source_locator->>'source' as detail, o.note, o.confidence
                   from core.observation o
                   where o.sample_id = %s
                      or o.subject_id = (select subject_id from core.sample where sample_id = %s)
                   order by (o.status = 'conflict') desc, o.registered desc, o.field""", (sample_id, sample_id))
        table(obs)
        st.caption("Runs")
        table(q("""select run_accession, assay, library_strategy, instrument_model, library_layout, spots,
                          processing_state, download_state, array_length(fastq_urls, 1) as fastq_files
                   from core.run where sample_id = %s""", (sample_id,)))

# ------------------------------------------------------------------ cohorts
elif page == "Cohorts":
    st.title("Cohorts")
    st.markdown("Case/control is decided only here, by named rules over the facts. The same samples can be in one cohort and not another.")
    defs = q("""select name, version, description, case_rule::text as cases, control_rule::text as controls,
                       exclude_rule::text as excludes, sample_types::text as sample_types
                from analysis.cohort_definition order by name, version""")
    table(defs)
    sizes = q("""select cohort, study_key as study,
                        count(*) filter (where label = 1) as cases, count(*) filter (where label = 0) as controls,
                        count(distinct cv_group) as "independent groups"
                 from analysis.ml_samples group by 1, 2 order by 1, 2""")
    for name, part in sizes.groupby("cohort"):
        st.subheader(name)
        c1, c2 = st.columns([2, 3])
        c1.metric("cases / controls", f"{part.cases.sum()} / {part.controls.sum()}")
        c2.bar_chart(part.set_index("study")[["cases", "controls"]], horizontal=True)
        table(part.drop(columns="cohort"))

# ------------------------------------------------------------------ fields
elif page == "Fields":
    st.title("Fields")
    st.markdown("Registered fields have a definition in `core.field_dictionary`. Unregistered ones are the long tail: "
                "kept as observations and promoted to columns once enough studies use them.")
    table(q("""select o.field, o.registered, count(distinct o.study_id) as studies, count(*) as values,
                      count(*) filter (where o.status = 'conflict') as conflicts,
                      d.promoted_to as "typed column", d.description
               from core.observation o left join core.field_dictionary d on d.field = o.field
               group by o.field, o.registered, d.promoted_to, d.description
               order by o.registered desc, studies desc, values desc"""), height=600)
    st.subheader("Raw archive fields per study")
    key = st.selectbox("Study", studies.study_key, key="raw_study")
    table(q("""select a.field_name as "archive column", count(distinct a.value) as "distinct values",
                      min(a.value) as example
               from raw.archive_attribute a
               join core.archive_project p on p.project_accession = a.project_accession
               join core.study s on s.study_id = p.study_id
               where s.study_key = %s group by 1 order by 2 desc""", (key,)), height=400)

# ------------------------------------------------------------------ open questions
elif page == "Open questions":
    st.title("Open questions for people")
    table(q("""select st.study_key as study, t.kind, t.title, t.what_is_missing as "what is missing",
                      t.why_blocked as why, t.suggested_action as "suggested action", t.priority, t.status
               from ops.human_task t join core.study st using (study_id) order by t.priority, st.study_key"""))
    st.subheader("Conflicting values")
    table(q("""select st.study_key as study, o.field, o.value_raw as "as found", o.value_norm as reading,
                      o.source_type as source, o.note, count(*) as values
               from core.observation o join core.study st using (study_id)
               where o.status = 'conflict' group by 1,2,3,4,5,6 order by 1,2,3"""))

# ------------------------------------------------------------------ bioinformatics
elif page == "Bioinformatics":
    st.title("Bioinformatics")
    table(q("""select st.study_key as study, r.assay, r.processing_state, r.download_state, count(*) as runs,
                      pg_size_pretty(sum((select sum(b) from unnest(r.fastq_bytes) b))) as fastq_size
               from core.run r join core.sample s using (sample_id) join core.study st using (study_id)
               group by 1,2,3,4 order by 1,2,3"""))
    pr = q("""select st.study_key as study, p.pipeline_name, p.pipeline_version, p.status, p.is_current,
                     p.started_at, p.finished_at
              from bio.pipeline_run p join core.study st using (study_id) order by p.started_at desc nulls last""")
    if pr.empty:
        st.info("No pipeline results loaded yet. The bioinformatics workflow fills bio.* (see db/README.md).")
    else:
        table(pr)
        st.subheader("Top genera")
        table(q("""select t.genus, sum(a.count) as reads, count(distinct a.run_accession) as runs
                   from bio.abundance a join bio.pipeline_run p on p.pipeline_run_id = a.pipeline_run_id and p.is_current
                   join bio.taxonomy_assignment t on t.asv_id = a.asv_id
                   group by 1 order by 2 desc limit 25"""))

# ------------------------------------------------------------------ model
elif page == "Model: IBD vs healthy":
    import json

    import altair as alt
    import numpy as np

    # Reference palette (dataviz skill, validated): categorical slot 1 and 2, neutral gray, diverging poles
    MODEL_COLORS = alt.Scale(domain=["logistic regression", "random forest"], range=["#2a78d6", "#eb6834"])
    NULL_GRAY = "#8a8985"
    MODEL_NAMES = {"logreg": "logistic regression", "forest": "random forest"}

    runs_dir = ROOT / "ml" / "results"
    runs = sorted([d for d in runs_dir.glob("*") if (d / "params.json").exists()], reverse=True) if runs_dir.exists() else []
    st.title("Model: IBD vs healthy")
    if not runs:
        st.info("No model results yet. Run `ml/loso.py` (see db/README.md).")
        st.stop()
    run = st.selectbox("Result set", runs, format_func=lambda d: d.name,
                       index=next((i for i, d in enumerate(runs) if d.name.endswith("ibd_vs_healthy_broad")), 0))
    params = json.loads((run / "params.json").read_text())
    loso = pd.read_csv(run / "metrics_loso.csv").assign(model=lambda d: d.model.map(MODEL_NAMES))
    within = pd.read_csv(run / "metrics_within_study.csv").assign(model=lambda d: d.model.map(MODEL_NAMES))
    perm = pd.read_csv(run / "permutation_loso_logreg.csv")
    coef = pd.read_csv(run / "coefficients_logreg.csv")
    pred = pd.read_csv(run / "predictions.csv").assign(model=lambda d: d.model.map(MODEL_NAMES))
    short = lambda k: k.split("-")[-1] if isinstance(k, str) else k  # noqa: E731

    st.markdown(
        "Can the database separate IBD from healthy **in a study the model has never seen?** Each study is held out "
        "in turn: the model trains on the other four and is scored on it (leave-one-study-out). "
        f"Features: genus-level counts from `analysis.genus_counts`, CLR. Cohort `{params['cohort']}`"
        + (f", {params['sample_type']} only" if params.get("sample_type") else ", all sample types")
        + f", at least {params['min_reads']} reads per sample. Write-up: `docs/ml-results.md`.")

    c = st.columns(5)
    for col, (m, short_m) in zip(c[:2], [("logistic regression", "LR"), ("random forest", "RF")]):
        col.metric(f"Held-out {short_m}", f"{loso[loso.model == m].auroc.mean():.2f}",
                   help=f"Mean AUROC of the {m} over the held-out studies.")
    c[2].metric("Within study", f"{within[within.model == 'logistic regression'].auroc.mean():.2f}",
                help="Mean AUROC of the logistic regression with grouped CV inside each study; the gap to the "
                     "held-out score is what crossing studies costs.")
    c[3].metric("Samples", params["n_samples"])
    c[4].metric("Genera", params["n_genera_total"])

    # --- held-out AUROC per study, both models, with the shuffled-label baseline
    st.subheader("Held-out AUROC per study")
    st.caption("Bars: AUROC on the held-out study. Gray tick: mean AUROC with labels shuffled (100 runs, logistic "
               "regression). Dashed line: chance (0.5).")
    df = loso.assign(study=loso.held_out.map(short),
                     label=lambda d: d.apply(lambda r: f"{short(r.held_out)} ({r.n_ibd}/{r.n_healthy})", axis=1))
    nulls = perm.assign(label=perm.held_out.map(dict(zip(df.held_out, df.label))))
    base = alt.Chart(df).encode(y=alt.Y("label:N", title="Held-out study (IBD/healthy)", sort=sorted(df.label.unique())))
    bars = base.mark_bar(cornerRadiusEnd=4, height={"band": 0.8}).encode(
        x=alt.X("auroc:Q", title="AUROC", scale=alt.Scale(domain=[0, 1])),
        yOffset=alt.YOffset("model:N"),
        color=alt.Color("model:N", scale=MODEL_COLORS, legend=alt.Legend(title=None, orient="top")),
        tooltip=[alt.Tooltip("held_out:N", title="study"), "model:N", alt.Tooltip("auroc:Q", format=".2f"),
                 alt.Tooltip("balanced_accuracy:Q", format=".2f", title="balanced accuracy"),
                 alt.Tooltip("sensitivity:Q", format=".2f"), alt.Tooltip("specificity:Q", format=".2f"), "n_ibd:Q", "n_healthy:Q"])
    ticks = alt.Chart(nulls).mark_tick(color=NULL_GRAY, thickness=3, size=26).encode(
        y="label:N", x="null_mean:Q",
        tooltip=[alt.Tooltip("held_out:N", title="study"), alt.Tooltip("null_mean:Q", format=".2f", title="shuffled mean"),
                 alt.Tooltip("null_p95:Q", format=".2f", title="shuffled 95th pct"), alt.Tooltip("p_value:Q", format=".3f")])
    chance = alt.Chart(pd.DataFrame({"x": [0.5]})).mark_rule(strokeDash=[4, 4], color=NULL_GRAY).encode(x="x:Q")
    st.altair_chart((bars + ticks + chance).properties(height=60 * df.label.nunique() + 40), use_container_width=True)

    # --- what crossing studies costs
    st.subheader("Within one study vs a new study (logistic regression)")
    st.caption("Gray: grouped CV inside the study. Blue: trained on the other studies. The drop is the batch effect "
               "between studies (region, platform, lab, cohort).")
    w = within[(within.model == "logistic regression") & within.auroc.notna()][["study", "auroc"]].assign(eval="within study")
    lo = loso[loso.model == "logistic regression"][["held_out", "auroc"]].rename(columns={"held_out": "study"}).assign(eval="held out")
    gap = pd.concat([w, lo]).assign(study=lambda d: d.study.map(short))
    gap_base = alt.Chart(gap).encode(y=alt.Y("study:N", title="Study"))
    line = gap_base.mark_line(color=NULL_GRAY, strokeWidth=2).encode(
        x=alt.X("auroc:Q", scale=alt.Scale(domain=[0.4, 1])), detail="study:N")
    dots = gap_base.mark_circle(size=140, opacity=1, stroke="#fcfcfb", strokeWidth=2).encode(
        x=alt.X("auroc:Q", title="AUROC", scale=alt.Scale(domain=[0.4, 1])),
        color=alt.Color("eval:N", scale=alt.Scale(domain=["within study", "held out"], range=[NULL_GRAY, "#2a78d6"]),
                        legend=alt.Legend(title=None, orient="top")),
        tooltip=["study:N", "eval:N", alt.Tooltip("auroc:Q", format=".2f")])
    st.altair_chart((line + dots).properties(height=50 * gap.study.nunique() + 40), use_container_width=True)

    # --- ROC curve for one held-out study
    st.subheader("ROC curve of a held-out study")

    def roc(y, p):
        order = np.argsort(-p)
        y = y[order]
        tpr = np.concatenate([[0], np.cumsum(y) / max(y.sum(), 1)])
        fpr = np.concatenate([[0], np.cumsum(1 - y) / max((1 - y).sum(), 1)])
        return pd.DataFrame({"false positive rate": fpr, "true positive rate": tpr})

    held = st.selectbox("Held-out study", sorted(loso.held_out.unique()), format_func=short)
    curves = []
    for m, part in pred[(pred["eval"] == "loso") & (pred.study_key == held)].groupby("model"):
        if 0 < part.label.sum() < len(part):
            curves.append(roc(part.label.to_numpy(), part.p_ibd.to_numpy()).assign(model=m))
    if curves:
        rc = alt.Chart(pd.concat(curves)).mark_line(strokeWidth=2).encode(
            x=alt.X("false positive rate:Q", scale=alt.Scale(domain=[0, 1])),
            y=alt.Y("true positive rate:Q", scale=alt.Scale(domain=[0, 1])),
            color=alt.Color("model:N", scale=MODEL_COLORS, legend=alt.Legend(title=None, orient="top")),
            tooltip=["model:N", alt.Tooltip("false positive rate:Q", format=".2f"), alt.Tooltip("true positive rate:Q", format=".2f")])
        diag = alt.Chart(pd.DataFrame({"false positive rate": [0, 1], "true positive rate": [0, 1]})).mark_line(
            strokeDash=[4, 4], color=NULL_GRAY).encode(x="false positive rate:Q", y="true positive rate:Q")
        st.altair_chart((rc + diag).properties(height=360, width=420))
    else:
        st.info("This study has only one class in the cohort, so it has no ROC curve.")

    # --- genera
    st.subheader("Genera the logistic regression leans on")
    st.caption("Coefficient on standardized CLR abundance, model fitted on all samples. Orange: more in IBD; "
               "blue: more in healthy. Hover for mean relative abundance in each group.")
    k = st.slider("Genera per side", 5, 25, 10)
    top = pd.concat([coef.head(k), coef.tail(k)]).assign(direction=lambda d: np.where(d.coef > 0, "higher in IBD", "higher in healthy"))
    gc = alt.Chart(top).mark_bar(cornerRadiusEnd=4).encode(
        y=alt.Y("genus:N", sort=alt.EncodingSortField("coef", order="descending"), title=None,
                axis=alt.Axis(labelOverlap=False, labelLimit=220)),
        x=alt.X("coef:Q", title="coefficient"),
        color=alt.Color("direction:N", scale=alt.Scale(domain=["higher in IBD", "higher in healthy"], range=["#eb6834", "#2a78d6"]),
                        legend=alt.Legend(title=None, orient="top")),
        tooltip=["genus:N", alt.Tooltip("coef:Q", format="+.2f"), alt.Tooltip("mean_rel_ibd:Q", format=".2%", title="mean in IBD"),
                 alt.Tooltip("mean_rel_healthy:Q", format=".2%", title="mean in healthy"), alt.Tooltip("n_studies:Q", title="studies")])
    st.altair_chart(gc.properties(height=24 * len(top) + 40), use_container_width=True)

    # --- tables
    with st.expander("Tables: metrics, predictions, parameters"):
        st.markdown("**Held-out (leave-one-study-out)**")
        table(loso)
        st.markdown("**Within-study grouped CV**")
        table(within)
        st.markdown("**Shuffled-label baseline (logistic regression)**")
        table(perm)
        st.markdown("**Per-sample predictions** (probability of IBD)")
        pick = st.selectbox("Study", ["all"] + sorted(pred.study_key.unique()), key="pred_study")
        table(pred if pick == "all" else pred[pred.study_key == pick], height=400)
        st.markdown("**Parameters**")
        st.json(params)
