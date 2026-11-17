-- Analysis layer: cohort definitions (the only place case/control is decided) and views.

-- One row per sample: facts only, with technical covariates. No case/control column.
create view analysis.sample_wide as
select
  s.sample_id, st.study_id, st.study_key, s.biosample_accession, s.archive_sample_name,
  s.subject_id, s.subject_link, subj.subject_key, subj.family_key,
  coalesce(subj.diagnosis, s.extra->>'diagnosis', sg.diagnosis_code,
           case when sg.role = 'control' then 'healthy' end)            as diagnosis,
  coalesce(subj.control_type, sg.control_type)                          as control_type,
  subj.sex, subj.age_years, subj.bmi, subj.country,
  sg.label as group_label, sg.role as group_role,
  s.sample_role, s.sample_type, s.body_site, s.inflamed, s.disease_activity, s.timepoint,
  s.used_in_paper, s.n_open_conflicts, s.label_source,
  coalesce(subj.extra, '{}'::jsonb) || s.extra                          as extra,
  r.n_runs, r.n_runs_16s, r.spots_16s,
  p.variable_region, p.platform, p.instrument_model, p.layout,
  q.reads_nonchim
from core.sample s
join core.study st on st.study_id = s.study_id
left join core.subject subj on subj.subject_id = s.subject_id
left join core.study_group sg on sg.group_id = s.group_id
left join lateral (
  select count(*) as n_runs,
         count(*) filter (where assay = '16s_amplicon') as n_runs_16s,
         sum(spots) filter (where assay = '16s_amplicon') as spots_16s,
         min(protocol_id) filter (where assay = '16s_amplicon') as protocol_id
  from core.run where sample_id = s.sample_id) r on true
left join core.protocol p on p.protocol_id = r.protocol_id
left join lateral (
  select sum(qc.reads_nonchim) as reads_nonchim
  from core.run ru
  join bio.run_qc qc on qc.run_accession = ru.run_accession
  join bio.pipeline_run pr on pr.pipeline_run_id = qc.pipeline_run_id and pr.is_current
  where ru.sample_id = s.sample_id) q on true;

create table analysis.cohort_definition (
  cohort_id    smallint generated always as identity primary key,
  name         text not null,
  version      smallint not null default 1,
  description  text not null,
  case_rule    jsonb not null,   -- {"diagnosis": [...]}
  control_rule jsonb not null,   -- {"diagnosis": [...], "exclude_control_types": [...]}
  exclude_rule jsonb not null default '{}',
     -- {"conflict_fields": [...], "subject_link": [...], "require_used_in_paper": true, "extra": {"key": "value"}}
  sample_types text[],
  min_reads    integer,
  created_by   text not null,
  created_at   timestamptz not null default now(),
  unique (name, version)
);

-- Samples each cohort selects, with label and cross-validation grouping keys.
create view analysis.ml_samples as
with latest as (
  select distinct on (name) * from analysis.cohort_definition order by name, version desc
)
select c.cohort_id, c.name as cohort, c.version as cohort_version,
       w.sample_id, w.study_id, w.study_key,
       case when w.diagnosis in (select jsonb_array_elements_text(c.case_rule->'diagnosis')) then 1 else 0 end as label,
       coalesce('family:' || w.study_id || ':' || w.family_key,
                'subject:' || w.subject_id,
                'study:' || w.study_id) as cv_group,
       w.diagnosis, w.control_type, w.sample_type, w.variable_region, w.platform, w.reads_nonchim
from latest c
join analysis.sample_wide w on true
where w.sample_role = 'biological'
  and w.n_runs_16s > 0
  and (c.sample_types is null or w.sample_type = any (c.sample_types))
  and (   w.diagnosis in (select jsonb_array_elements_text(c.case_rule->'diagnosis'))
       or (    w.diagnosis in (select jsonb_array_elements_text(c.control_rule->'diagnosis'))
           and coalesce(w.control_type, '') not in (select jsonb_array_elements_text(coalesce(c.control_rule->'exclude_control_types', '[]')))))
  and not (coalesce(c.exclude_rule->'subject_link', '[]') ? w.subject_link)
  and not (coalesce((c.exclude_rule->>'require_used_in_paper')::boolean, false) and w.used_in_paper is not true)
  and not exists (select 1 from jsonb_each(coalesce(c.exclude_rule->'extra', '{}')) e where w.extra->e.key = e.value)
  and not exists (
        select 1 from core.observation o
        where o.status = 'conflict'
          and o.field in (select jsonb_array_elements_text(coalesce(c.exclude_rule->'conflict_fields', '[]')))
          and (o.sample_id = w.sample_id or o.subject_id = w.subject_id))
  and (c.min_reads is null or w.reads_nonchim is null or w.reads_nonchim >= c.min_reads);

-- Contract for the bioinformatics workflow: 16S runs still to process.
create view analysis.samplesheet_16s as
select r.run_accession, r.sample_id, st.study_key, st.study_id,
       r.protocol_id, p.variable_region, p.forward_primer_seq, p.reverse_primer_seq,
       coalesce(r.platform, p.platform) as platform, r.instrument_model,
       coalesce(r.library_layout, p.layout) as layout,
       r.spots, r.fastq_urls, r.fastq_md5, r.fastq_bytes,
       r.processing_state, r.download_state, r.fastq_local_path
from core.run r
join core.sample s on s.sample_id = r.sample_id
join core.study st on st.study_id = s.study_id
left join core.protocol p on p.protocol_id = r.protocol_id
where r.assay = '16s_amplicon' and r.processing_state in ('queued', 'failed');

-- One row per study for the dashboard.
create view analysis.study_progress as
select st.study_id, st.study_key, st.curation_state, st.verdict, st.blocked_reason,
  (select count(*) from core.sample s where s.study_id = st.study_id) as n_samples,
  (select count(*) from core.sample s where s.study_id = st.study_id and s.group_id is not null) as n_with_group,
  (select count(*) from core.sample s where s.study_id = st.study_id and s.n_open_conflicts > 0) as n_with_conflicts,
  (select count(*) from core.run r join core.sample s using (sample_id) where s.study_id = st.study_id) as n_runs,
  (select count(*) from core.run r join core.sample s using (sample_id) where s.study_id = st.study_id and r.assay = '16s_amplicon') as n_runs_16s,
  (select count(*) from core.run r join core.sample s using (sample_id) where s.study_id = st.study_id and r.processing_state = 'processed') as n_runs_processed,
  (select count(*) from ops.validation_check v where v.study_id = st.study_id and v.result = 'pass') as n_checks_pass,
  (select count(*) from ops.validation_check v where v.study_id = st.study_id and v.result = 'fail') as n_checks_fail,
  (select count(*) from ops.validation_check v where v.study_id = st.study_id and v.result = 'not_comparable') as n_checks_not_comparable,
  (select count(*) from ops.human_task t where t.study_id = st.study_id and t.status in ('open','in_progress')) as n_open_tasks,
  (select max(at) from ops.activity_log l where l.study_id = st.study_id) as last_activity_at
from core.study st;
