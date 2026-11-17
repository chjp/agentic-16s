-- Quick look at what is in the database.  psql -d agentic16s -f db/queries/overview.sql

\echo '== Studies'
select study_key, verdict, n_samples, n_with_group, n_runs, n_runs_16s,
       n_checks_pass as pass, n_checks_fail as fail, n_checks_not_comparable as n_a, n_open_tasks as tasks
from analysis.study_progress order by study_key;

\echo '== 16S runs: group, sex and age known (biological samples)'
select st.study_key,
       count(*) as runs_16s,
       count(*) filter (where s.group_id is not null) as with_group,
       count(*) filter (where subj.sex is not null) as with_sex,
       count(*) filter (where subj.age_years is not null) as with_age,
       count(*) filter (where s.n_open_conflicts > 0) as with_conflict
from core.run r join core.sample s using (sample_id) join core.study st using (study_id)
left join core.subject subj on subj.subject_id = s.subject_id
where r.assay = '16s_amplicon'
group by rollup (st.study_key) order by st.study_key nulls last;

\echo '== The paper''s own groups'
select st.study_key, g.label, g.role, g.diagnosis_code, g.control_type, count(s.*) as samples
from core.study_group g join core.study st using (study_id)
left join core.sample s on s.group_id = g.group_id
group by 1,2,3,4,5 order by 1,2;

\echo '== Observations by source type and status'
select source_type, status, count(*) from core.observation group by 1,2 order by 1,2;

\echo '== Long-tail fields not in the dictionary (promotion candidates)'
select field, count(distinct study_id) as studies, count(*) as values
from core.observation where not registered group by field order by studies desc, values desc limit 15;

\echo '== Open conflicts'
select st.study_key, o.field, count(distinct o.conflict_group) as entities
from core.observation o join core.study st using (study_id) where o.status = 'conflict' group by 1,2;

\echo '== Open tasks for people'
select st.study_key, t.kind, t.title from ops.human_task t join core.study st using (study_id) where t.status = 'open';

\echo '== Runs by processing state (what the bioinformatics workflow will pick up)'
select assay, processing_state, count(*) from core.run group by 1,2 order by 1,2;

\echo '== Cohort sizes'
select cohort, label, count(*) as samples, count(distinct cv_group) as cv_groups, count(distinct study_id) as studies
from analysis.ml_samples group by 1,2 order by 1,2 desc;
