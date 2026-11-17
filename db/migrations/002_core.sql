-- Curated layer: facts with provenance. Case/control is NOT decided here (see analysis.cohort_definition).

create table core.disease (
  disease_id   smallint generated always as identity primary key,
  code         text not null unique,
  name         text not null,
  parent_id    smallint references core.disease,
  ontology_id  text,
  synonyms     text[],
  search_query text
);

create table core.paper (
  paper_id        bigint generated always as identity primary key,
  pmid            text unique,
  pmcid           text unique,
  doi             text unique,
  title           text not null,
  journal         text,
  pub_year        smallint,
  pub_date        date,
  authors         jsonb,
  abstract        text,
  is_open_access  boolean,
  oa_license      text,
  fulltext_status text not null default 'not_tried'
    check (fulltext_status in ('not_tried','fetched_oa','not_oa','blocked','human_supplied','unavailable')),
  found_by        jsonb,
  raw_record_id   bigint,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index on core.paper (pub_year);

create table core.study (
  study_id           bigint generated always as identity primary key,
  study_key          text not null unique,
  primary_paper_id   bigint references core.paper,
  disease_id         smallint not null references core.disease,
  title              text,
  design             text check (design in ('case_control','cross_sectional','cohort_longitudinal','intervention','other')),
  screening_decision text not null default 'undecided' check (screening_decision in ('undecided','include','exclude')),
  screening_reason   text,
  curation_state     text not null default 'discovered'
    check (curation_state in ('discovered','screened','metadata_extracted','labels_located','labels_validated','processed','loaded')),
  blocked            boolean not null default false,
  blocked_reason     text,
  case_definition    text,
  control_definition text,
  family_based       boolean,
  countries          text[],
  recruitment_setting text,
  collection_period  text,
  verdict            text check (verdict in ('complete','partial','failed')),
  verdict_reason     text,
  notes              text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create index on core.study (curation_state);

create table core.study_paper (
  study_id bigint references core.study on delete cascade,
  paper_id bigint references core.paper,
  role     text not null check (role in ('primary','companion','reuse','erratum')),
  evidence text,
  primary key (study_id, paper_id)
);

create table core.study_group (
  group_id        bigint generated always as identity primary key,
  study_id        bigint not null references core.study on delete cascade,
  label           text not null,
  definition      text,
  evidence_quote  text,
  diagnosis_code  text references core.disease (code),
  role            text check (role in ('case','control','comparator','other')),
  control_type    text check (control_type in ('healthy_volunteer','family_member','symptomatic_non_ibd','household_member','other')),
  parent_group_id bigint references core.study_group,
  extra           jsonb not null default '{}',
  unique (study_id, label)
);

create table core.document (
  document_id    bigint generated always as identity primary key,
  paper_id       bigint references core.paper,
  study_id       bigint references core.study on delete cascade,
  kind           text not null check (kind in ('fulltext_xml','fulltext_pdf','supplement','external_metadata','author_correspondence','archive_attachment','other')),
  original_name  text,
  source_url     text,
  origin         text not null check (origin in ('publisher','pmc','europepmc','author_site','data_repository','third_party_mirror','author_email','human_upload')),
  obtained_by    text not null check (obtained_by in ('agent','script','human')),
  license        text,
  redistributable boolean not null default false,
  storage_path   text not null,
  sha256         text not null unique,
  bytes          bigint,
  mime_type      text,
  retrieved_at   timestamptz not null default now(),
  log_id         bigint
);

create table core.reported_count (
  count_id             bigint generated always as identity primary key,
  study_id             bigint not null references core.study on delete cascade,
  name                 text not null,
  unit                 text not null check (unit in ('subjects','samples','runs','reads','families','other')),
  group_id             bigint references core.study_group on delete set null,
  filter               jsonb,
  value                numeric,
  value_text           text,   -- when the paper states something that is not a single number
  stat                 text not null default 'count' check (stat in ('count','mean','median','sd','percent','min','max','other')),
  evidence_quote       text,
  evidence_document_id bigint references core.document on delete set null,
  evidence_location    text,
  extraction_id        bigint,
  unique (study_id, name)
);

create table core.archive_project (
  project_accession   text primary key,
  study_id            bigint references core.study on delete set null,
  secondary_accession text,
  archive             text not null check (archive in ('ncbi','ena','ddbj')),
  title               text,
  description         text,
  center_name         text,
  n_runs_archive      integer,
  n_biosamples_archive integer,
  first_release       date,
  archive_pubmed_ids  text[],
  raw_record_id       bigint
);
create index on core.archive_project (study_id);

create table core.protocol (
  protocol_id          bigint generated always as identity primary key,
  study_id             bigint not null references core.study on delete cascade,
  label                text not null,
  target_gene          text not null default '16S rRNA',
  variable_region      text,
  forward_primer_name  text,
  forward_primer_seq   text,
  reverse_primer_name  text,
  reverse_primer_seq   text,
  primers_in_reads     boolean,
  platform             text,
  instrument_model     text,
  layout               text check (layout in ('PAIRED','SINGLE')),
  read_length_nominal  smallint,
  sequencing_kit       text,
  dna_extraction_kit   text,
  bead_beating         boolean,
  pcr_polymerase       text,
  pcr_cycles           smallint,
  sample_storage       text,
  sequencing_center    text,
  negative_controls_sequenced boolean,
  mock_community_sequenced    boolean,
  extra                jsonb not null default '{}',
  unique (study_id, label)
);
create index on core.protocol (variable_region);

create table core.subject (
  subject_id         bigint generated always as identity primary key,
  study_id           bigint not null references core.study on delete cascade,
  subject_key        text not null,
  subject_key_source text not null check (subject_key_source in ('archive','paper','inferred_rule','one_per_sample')),
  subject_key_rule   text,
  family_key         text,
  group_id           bigint references core.study_group on delete set null,
  diagnosis          text,   -- disease code (cd, uc, ibdu, ibd) or healthy / non_ibd_control / other
  control_type       text check (control_type in ('healthy_volunteer','family_member','symptomatic_non_ibd','household_member','other')),
  sex                text check (sex in ('female','male','other')),
  age_years          numeric(5,1),
  age_range          text,
  bmi                numeric(4,1),
  country            text,
  ethnicity          text,
  diagnosis_detail   jsonb not null default '{}',
  extra              jsonb not null default '{}',
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (study_id, subject_key)
);
create index on core.subject (study_id, family_key);

create table core.sample (
  sample_id            bigint generated always as identity primary key,
  study_id             bigint not null references core.study on delete cascade,
  subject_id           bigint references core.subject on delete set null,
  subject_link         text not null check (subject_link in ('known','inferred','one_per_sample','unknown','not_applicable')),
  group_id             bigint references core.study_group on delete set null,
  used_in_paper        boolean,
  biosample_accession  text unique,
  archive_sample_name  text,
  paper_sample_name    text,
  sample_role          text not null default 'biological'
    check (sample_role in ('biological','negative_control','mock_community','positive_control','technical_pool','other')),
  sample_type          text,
  body_site            text,
  body_site_uberon     text,
  inflamed             boolean,
  disease_activity     text check (disease_activity in ('active','remission','unknown')),
  activity_scores      jsonb,
  timepoint            text,
  days_from_baseline   integer,
  collection_date      date,
  antibiotics_recent   boolean,
  medications          jsonb,
  label_source         text,
  n_open_conflicts     smallint not null default 0,
  extra                jsonb not null default '{}',
  raw_record_id        bigint,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  check (subject_id is not null or subject_link in ('unknown','not_applicable'))
);
create index on core.sample (study_id);
create index on core.sample (subject_id);

create table core.run (
  run_accession        text primary key,
  sample_id            bigint not null references core.sample on delete cascade,
  experiment_accession text,
  project_accession    text not null references core.archive_project,
  protocol_id          bigint references core.protocol on delete set null,
  platform             text,
  instrument_model     text,
  library_strategy     text,
  library_source       text,
  library_selection    text,
  library_layout       text,
  assay                text not null check (assay in ('16s_amplicon','shotgun','other_amplicon','unknown')),
  used_in_paper        boolean,
  spots                bigint,
  bases                bigint,
  avg_length_archive   integer,
  observed_read_length smallint,
  release_date         timestamptz,
  fastq_urls           text[],
  fastq_md5            text[],
  fastq_bytes          bigint[],
  fastq_local_path     text,
  processing_state     text not null default 'queued'
    check (processing_state in ('queued','processed','skipped_not_16s','skipped_not_used','failed')),
  download_state       text not null default 'not_downloaded'
    check (download_state in ('not_downloaded','downloaded','failed','deleted')),
  fastq_deleted_at     timestamptz,
  raw_record_id        bigint,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  -- raw FASTQ may only be deleted once its results are processed and loaded
  check (download_state <> 'deleted' or processing_state = 'processed'),
  check ((download_state = 'deleted') = (fastq_deleted_at is not null))
);
create index on core.run (sample_id);
create index on core.run (assay, processing_state);

create table core.field_dictionary (
  field          text primary key,
  level          text not null check (level in ('subject','sample','run')),
  value_type     text not null check (value_type in ('text','enum','number','boolean','date')),
  allowed_values text[],
  unit           text,
  synonyms       text[],
  description    text not null,
  promoted_to    text,
  added_at       date not null default current_date
);

create table core.value_map (
  value_map_id bigint generated always as identity primary key,
  study_id     bigint not null references core.study on delete cascade,
  source_field text not null,
  raw_value    text not null,
  field        text not null,
  value_norm   text,
  also_sets    jsonb,
  evidence     text,
  basis        text not null check (basis in ('stated','count_match','assumed')),
  status       text not null default 'proposed' check (status in ('proposed','accepted','rejected')),
  log_id       bigint
);

create table core.extraction (
  extraction_id        bigint generated always as identity primary key,
  study_id             bigint not null references core.study on delete cascade,
  protocol_id          bigint references core.protocol on delete set null,
  field                text not null,
  value                text,
  value_json           jsonb,
  not_reported         boolean not null default false,
  evidence_quote       text,
  evidence_document_id bigint references core.document on delete set null,
  evidence_location    jsonb,
  quote_verified       boolean,
  extractor            text not null,
  llm_call_id          bigint,
  schema_version       text not null,
  is_gold              boolean not null default false,
  status               text not null default 'proposed' check (status in ('proposed','accepted','rejected','superseded')),
  reviewed_by          text,
  model_confidence     numeric(3,2)
);
create index on core.extraction (study_id);

create table core.sample_name_map (
  map_id              bigint generated always as identity primary key,
  study_id            bigint not null references core.study on delete cascade,
  paper_sample_name   text not null,
  source_document_id  bigint references core.document on delete set null,
  biosample_accession text,
  run_accession       text,
  match_method        text not null check (match_method in ('exact','normalized','pattern','llm','human')),
  matched_on          text,
  rule                text,
  confidence          numeric(3,2),
  status              text not null default 'candidate' check (status in ('candidate','accepted','rejected')),
  log_id              bigint,
  check (biosample_accession is not null or run_accession is not null)
);

create table core.observation (
  obs_id             bigint generated always as identity primary key,
  study_id           bigint not null references core.study on delete cascade,
  entity_type        text not null check (entity_type in ('subject','sample','run')),
  subject_id         bigint references core.subject on delete cascade,
  sample_id          bigint references core.sample on delete cascade,
  run_accession      text references core.run on delete cascade,
  field              text not null,
  registered         boolean not null default false,
  value_raw          text,
  value_norm         text,
  value_num          numeric,
  unit               text,
  source_type        text not null check (source_type in ('archive_attribute','archive_name_pattern','archive_title','supplement','paper_table','paper_text','paper_figure','external_file','author_contact','human','derived')),
  source_document_id bigint references core.document on delete set null,
  source_locator     jsonb,
  value_map_id       bigint references core.value_map on delete set null,
  status             text not null default 'candidate' check (status in ('candidate','accepted','rejected','conflict','superseded')),
  conflict_group     bigint,
  confidence         text check (confidence in ('high','medium','low')),
  note               text,
  llm_call_id        bigint,
  log_id             bigint,
  created_at         timestamptz not null default now(),
  check (num_nonnulls(subject_id, sample_id, run_accession) = 1),
  check ((entity_type = 'subject') = (subject_id is not null)
     and (entity_type = 'sample') = (sample_id is not null)
     and (entity_type = 'run') = (run_accession is not null))
);
create index on core.observation (study_id, field);
create index on core.observation (status);
-- at most one accepted value per entity and field
create unique index observation_one_accepted
  on core.observation (entity_type, coalesce(subject_id, 0), coalesce(sample_id, 0), coalesce(run_accession, ''), field)
  where status = 'accepted';

do $$ declare t text; begin
  foreach t in array array['paper','study','subject','sample','run'] loop
    execute format('create trigger touch before update on core.%I for each row execute function ops.touch_updated_at()', t);
  end loop;
end $$;
