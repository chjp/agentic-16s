-- Layers of the agentic-16s database (schema-design.html, draft v0.2).
create schema if not exists raw;       -- verbatim copies of everything fetched or generated
create schema if not exists core;      -- curated facts with provenance
create schema if not exists bio;       -- bioinformatics output (filled by the Nextflow workflow)
create schema if not exists ops;       -- activity log, human tasks, validation checks
create schema if not exists analysis;  -- cohort definitions and the views built from them

-- updated_at maintenance for core and bio tables
create function ops.touch_updated_at() returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;
