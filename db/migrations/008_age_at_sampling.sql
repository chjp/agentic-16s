-- Age is a property of the sample (age at sampling): one person sampled at several visits
-- has several ages (23013615). core.subject.age_years becomes the age at the first sample.

alter table core.sample add column age_at_sampling numeric(5,1);

update core.field_dictionary
   set level = 'sample',
       description = 'Age at sampling. core.subject.age_years holds the age at the person''s first sample.',
       promoted_to = 'core.sample.age_at_sampling'
 where field = 'age_years';

create or replace view analysis.sample_wide as
select
  s.sample_id, st.study_id, st.study_key, s.biosample_accession, s.archive_sample_name,
  s.subject_id, s.subject_link, subj.subject_key, subj.family_key,
  coalesce(subj.diagnosis, s.extra->>'diagnosis', sg.diagnosis_code,
           case when sg.role = 'control' then 'healthy' end)            as diagnosis,
  coalesce(subj.control_type, sg.control_type)                          as control_type,
  subj.sex, coalesce(s.age_at_sampling, subj.age_years) as age_years, subj.bmi, subj.country,
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
