-- Controlled lists and the field dictionary, plus two example cohorts.

insert into core.disease (code, name, synonyms) values
  ('ibd',  'Inflammatory bowel disease', '{IBD}');
insert into core.disease (code, name, parent_id, synonyms)
select v.code, v.name, d.disease_id, v.syn from core.disease d,
  (values ('cd',   'Crohn''s disease',          '{CD,Crohn,"Crohn''s disease",CD_F,CD_M}'::text[]),
          ('uc',   'Ulcerative colitis',        '{UC,UCN,UCP,"ulcerative colitis"}'::text[]),
          ('ibdu', 'IBD unclassified / indeterminate colitis', '{IBDU,IC,Indeterminate}'::text[])) v(code, name, syn)
where d.code = 'ibd';

insert into core.field_dictionary (field, level, value_type, allowed_values, unit, synonyms, description, promoted_to) values
  ('diagnosis', 'subject', 'enum', '{cd,uc,ibdu,ibd,healthy,non_ibd_control,other}', null,
     '{disease,gastrointest_disord,"disease state",dx,host_disease,DiseaseState}', 'Diagnosis of the person.', 'core.subject.diagnosis'),
  ('sex', 'subject', 'enum', '{female,male,other}', null, '{host_sex,gender,Sex}', 'Sex as reported.', 'core.subject.sex'),
  ('age_years', 'subject', 'number', null, 'years', '{age,host_age,Age}', 'Age at sampling or enrolment.', 'core.subject.age_years'),
  ('control_type', 'subject', 'enum', '{healthy_volunteer,family_member,symptomatic_non_ibd,household_member,other}', null, null,
     'Kind of control, for control subjects.', 'core.subject.control_type'),
  ('family_key', 'subject', 'text', null, null, '{family_id,family}', 'Family id when relatives are enrolled.', 'core.subject.family_key'),
  ('relationship_to_proband', 'subject', 'enum', '{proband,sibling,parent,child,spouse,other}', null,
     '{host_family_relationship,family_role,relation}', 'Relationship to the index patient in family-based studies.', null),
  ('group_paper', 'sample', 'text', null, null, '{group,sample_group}', 'The paper''s own group label for the sample.', 'core.sample.group_id'),
  ('sample_type', 'sample', 'enum', '{stool,mucosal_biopsy,rectal_swab,saliva,other}', null, '{env_medium,isolation_source,tissue}', 'Specimen type.', 'core.sample.sample_type'),
  ('body_site', 'sample', 'text', null, null, '{biopsy_site,bioploc,body_site}', 'Body site of the specimen (biopsies).', 'core.sample.body_site'),
  ('inflamed', 'sample', 'boolean', null, null, '{inflammation}', 'Whether the sampled tissue was inflamed.', 'core.sample.inflamed'),
  ('timepoint', 'sample', 'text', null, null, '{visit,time}', 'Timepoint label in longitudinal studies.', 'core.sample.timepoint'),
  ('treatment', 'sample', 'text', null, null, '{primary_induction,medication}', 'Treatment at or around sampling, as reported.', null),
  ('treatment_response', 'subject', 'enum', '{responder,non_responder,control}', null, '{"treatment response"}', 'Response to treatment as the study defines it.', null),
  ('c_difficile_infection', 'sample', 'boolean', null, null, null, 'Clostridioides difficile infection at sampling.', null),
  ('disease_activity', 'sample', 'enum', '{active,remission,unknown}', null, '{active}', 'Disease activity at sampling.', 'core.sample.disease_activity'),
  ('smoker', 'subject', 'text', null, null, '{smoker_code,smoking}', 'Smoking status as reported.', null),
  ('used_in_paper', 'sample', 'boolean', null, null, '{used_in_paper_16s_analysis}', 'Whether the authors analysed the sample.', 'core.sample.used_in_paper');

insert into analysis.cohort_definition (name, description, case_rule, control_rule, exclude_rule, sample_types, created_by) values
  ('ibd_vs_healthy_broad',
   'Any IBD sample vs any healthy control, every sample type. Maximises size.',
   '{"diagnosis": ["cd","uc","ibdu","ibd"]}',
   '{"diagnosis": ["healthy"]}',
   '{"conflict_fields": ["diagnosis"]}',
   null, 'agent:setup'),
  ('ibd_vs_healthy_strict_stool',
   'IBD stool vs stool of unrelated healthy controls; only samples the authors used; no C. difficile co-infection; no unknown subject links.',
   '{"diagnosis": ["cd","uc","ibdu","ibd"]}',
   '{"diagnosis": ["healthy"], "exclude_control_types": ["family_member"]}',
   '{"conflict_fields": ["diagnosis"], "subject_link": ["unknown"], "require_used_in_paper": true, "extra": {"c_difficile_infection": "yes"}}',
   '{stool}', 'agent:setup');
