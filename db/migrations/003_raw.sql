-- Raw layer: verbatim copies. Never edited, only appended.

create table raw.api_record (
  record_id       bigint generated always as identity primary key,
  source          text not null,   -- epmc_search, pubmed, sra_runinfo, biosample, ena_filereport, curated_table, ...
  accession       text,
  request         text not null,   -- URL, query, or file path the payload came from
  http_status     smallint,
  content_type    text,
  payload_json    jsonb,
  payload_text    text,
  payload_sha256  text,
  fetched_at      timestamptz not null default now(),
  last_seen_at    timestamptz not null default now(),
  fetcher_version text
);
create index on raw.api_record (accession);
create index on raw.api_record (payload_sha256);

-- Every archive field flattened: BioSample attributes, sample names, experiment titles, run tables.
create table raw.archive_attribute (
  accession         text not null,
  entity_type       text not null check (entity_type in ('biosample','experiment','run','ena_sample')),
  field_name        text not null,
  record_id         bigint not null references raw.api_record on delete cascade,
  harmonized_name   text,
  value             text,
  project_accession text,
  primary key (accession, field_name, record_id)
);
create index on raw.archive_attribute (project_accession, field_name);
create index on raw.archive_attribute (entity_type);

create table raw.supplement_table (
  table_id       bigint generated always as identity primary key,
  document_id    bigint not null references core.document on delete cascade,
  sheet_or_table text,
  table_index    smallint not null,
  n_rows         integer,
  n_cols         integer,
  header         jsonb,
  rows           jsonb,
  parser         text not null,
  needs_review   boolean not null default false,
  parse_warnings text[]
);

create table raw.llm_call (
  llm_call_id    bigint generated always as identity primary key,
  purpose        text not null,
  study_id       bigint references core.study on delete set null,
  provider       text not null,
  model          text not null,
  model_params   jsonb,
  prompt_name    text not null,
  prompt_version text not null,
  schema_version text,
  request        jsonb not null,
  response       jsonb not null,
  output_parsed  jsonb,
  parse_ok       boolean not null,
  parse_error    text,
  input_tokens   integer,
  output_tokens  integer,
  cached_tokens  integer,
  cost_usd       numeric(10,5),
  latency_ms     integer,
  orchestrator   text,
  trace_id       text,
  created_at     timestamptz not null default now()
);
create index on raw.llm_call (purpose);

-- deferred foreign keys from core to raw
alter table core.paper           add foreign key (raw_record_id) references raw.api_record on delete set null;
alter table core.archive_project add foreign key (raw_record_id) references raw.api_record on delete set null;
alter table core.sample          add foreign key (raw_record_id) references raw.api_record on delete set null;
alter table core.run             add foreign key (raw_record_id) references raw.api_record on delete set null;
alter table core.extraction      add foreign key (llm_call_id) references raw.llm_call on delete set null;
alter table core.observation     add foreign key (llm_call_id) references raw.llm_call on delete set null;
