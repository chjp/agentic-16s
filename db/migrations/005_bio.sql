-- Bioinformatics layer, filled by the Nextflow (nf-core/ampliseq) workflow.

create table bio.reference_db (
  ref_db_id smallint generated always as identity primary key,
  name      text not null,
  version   text not null,
  file_url  text,
  sha256    text,
  released  date,
  unique (name, version)
);

create table bio.pipeline_run (
  pipeline_run_id    bigint generated always as identity primary key,
  study_id           bigint not null references core.study on delete cascade,
  protocol_id        bigint not null references core.protocol on delete cascade,
  pipeline_name      text not null,
  pipeline_version   text not null,
  pipeline_commit    text,
  nextflow_version   text,
  params             jsonb not null,
  containers         jsonb,
  ref_db_id          smallint references bio.reference_db,
  samplesheet_sha256 text,
  host               text,
  started_at         timestamptz,
  finished_at        timestamptz,
  status             text not null check (status in ('running','succeeded','failed','superseded')),
  error_message      text,
  is_current         boolean not null default false,
  report_path        text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create unique index pipeline_run_one_current on bio.pipeline_run (protocol_id) where is_current;
create trigger touch before update on bio.pipeline_run for each row execute function ops.touch_updated_at();

create table bio.run_qc (
  pipeline_run_id  bigint references bio.pipeline_run on delete cascade,
  run_accession    text references core.run on delete cascade,
  reads_input      bigint,
  pct_primer_found numeric(5,2),
  reads_after_trim bigint,
  reads_filtered   bigint,
  reads_denoised   bigint,
  reads_merged     bigint,
  reads_nonchim    bigint,
  status           text not null check (status in ('ok','low_depth','failed')),
  failure_reason   text,
  primary key (pipeline_run_id, run_accession)
);

create table bio.asv (
  asv_id                bigint generated always as identity primary key,
  sequence              text not null,
  sequence_md5          text not null unique,
  length                smallint not null,
  region_inferred       text,
  first_pipeline_run_id bigint references bio.pipeline_run on delete set null
);

create table bio.abundance (
  pipeline_run_id bigint references bio.pipeline_run on delete cascade,
  run_accession   text references core.run on delete cascade,
  asv_id          bigint references bio.asv,
  count           integer not null check (count > 0),
  primary key (pipeline_run_id, run_accession, asv_id)
);
create index on bio.abundance (asv_id);

create table bio.taxonomy_assignment (
  asv_id          bigint references bio.asv on delete cascade,
  ref_db_id       smallint references bio.reference_db,
  classifier      text,
  domain          text,
  phylum          text,
  class           text,
  order_name      text,
  family          text,
  genus           text,
  species         text,
  confidence      jsonb,
  min_bootstrap   smallint,
  pipeline_run_id bigint references bio.pipeline_run on delete set null,
  primary key (asv_id, ref_db_id, classifier)
);
create index on bio.taxonomy_assignment (genus);
