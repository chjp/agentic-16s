-- Operations: activity log, human task queue, validation checks.

create table ops.activity_log (
  log_id        bigint generated always as identity primary key,
  at            timestamptz not null default now(),
  study_id      bigint references core.study on delete cascade,
  entity_type   text,
  entity_id     text,
  actor_type    text not null check (actor_type in ('agent','script','human')),
  actor         text not null,
  session_id    text,
  step          text,
  action        text not null,
  input         jsonb,
  outcome       text not null check (outcome in ('success','failure','blocked','skipped','partial')),
  reason        text,
  detail        jsonb,
  llm_call_id   bigint references raw.llm_call on delete set null,
  parent_log_id bigint references ops.activity_log,
  duration_ms   integer
);
create index on ops.activity_log (study_id, at);
create index on ops.activity_log (action);
create index on ops.activity_log (session_id);

create table ops.human_task (
  task_id                bigint generated always as identity primary key,
  study_id               bigint references core.study on delete cascade,
  kind                   text not null check (kind in ('obtain_fulltext','obtain_supplement','contact_authors','resolve_conflict','review_value_map','review_transcription','review_extraction','review_name_map','other')),
  title                  text not null,
  what_is_missing        text not null,
  why_blocked            text,
  suggested_action       text,
  priority               smallint not null default 2 check (priority between 1 and 3),
  status                 text not null default 'open' check (status in ('open','in_progress','done','wont_do')),
  resume_stage           text,
  resolution             text,
  resolution_document_id bigint references core.document on delete set null,
  related                jsonb,
  opened_log_id          bigint references ops.activity_log on delete set null,
  closed_log_id          bigint references ops.activity_log on delete set null,
  opened_at              timestamptz not null default now(),
  closed_at              timestamptz
);
create index on ops.human_task (status);

create table ops.validation_check (
  check_id   bigint generated always as identity primary key,
  study_id   bigint not null references core.study on delete cascade,
  attempt    smallint not null default 1,
  check_name text not null,
  count_id   bigint references core.reported_count on delete cascade,
  expected   numeric,
  observed   numeric,
  expected_text text,
  observed_text text,
  tolerance  numeric,
  result     text not null check (result in ('pass','fail','not_comparable')),
  detail     jsonb,
  log_id     bigint references ops.activity_log on delete set null,
  checked_at timestamptz not null default now()
);
create index on ops.validation_check (study_id, result);

alter table core.document    add foreign key (log_id) references ops.activity_log on delete set null;
alter table core.value_map   add foreign key (log_id) references ops.activity_log on delete set null;
alter table core.observation add foreign key (log_id) references ops.activity_log on delete set null;
alter table core.sample_name_map add foreign key (log_id) references ops.activity_log on delete set null;

-- the activity log is append-only for application roles; the owner can still reload a study
comment on table ops.activity_log is 'Append-only record of every action by agents, scripts and people.';
