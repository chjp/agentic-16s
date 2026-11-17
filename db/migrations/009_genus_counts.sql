-- Genus-level counts per sample: the feature table for cross-study models (Phase 6.1).
-- Studies use different 16S regions (V3-V5, V4, V6-V8, V3-V4), so no ASV is shared between them;
-- genus is the level where they meet (schema-design.html, analysis.genus_counts).
--
-- Rules:
-- * only the current pipeline run of each protocol (bio.pipeline_run.is_current);
-- * off-target reads are dropped here, not in the pipeline (exclude_taxa none keeps counts complete):
--   mitochondria, chloroplasts, and ASVs without a domain;
-- * names are cleaned with analysis.clean_taxon (SILVA 144 via ampliseq marks ambiguous calls "X--other");
-- * reads without a genus go to the lowest assigned rank plus "_unclassified" (Lachnospiraceae_unclassified);
-- * counts are summed over a sample's runs; rel_abundance = count / sample total after the filters.
-- Refresh after every bio load:  refresh materialized view analysis.genus_counts;

create or replace function analysis.clean_taxon(name text) returns text
language sql immutable as $$
  select nullif(regexp_replace(btrim(coalesce(name, '')), '--other$', ''), '')
$$;

create materialized view analysis.genus_counts as
with kept as (
  select r.sample_id, a.pipeline_run_id, t.ref_db_id, a.count,
         coalesce(analysis.clean_taxon(t.genus),
                  analysis.clean_taxon(t.family)     || '_unclassified',
                  analysis.clean_taxon(t.order_name) || '_unclassified',
                  analysis.clean_taxon(t.class)      || '_unclassified',
                  analysis.clean_taxon(t.phylum)     || '_unclassified',
                  analysis.clean_taxon(t.domain)     || '_unclassified') as genus
  from bio.abundance a
  join bio.pipeline_run pr on pr.pipeline_run_id = a.pipeline_run_id and pr.is_current
  join bio.taxonomy_assignment t on t.asv_id = a.asv_id and t.pipeline_run_id = a.pipeline_run_id
  join core.run r on r.run_accession = a.run_accession
  where analysis.clean_taxon(t.domain) is not null
    and coalesce(t.family, '') <> 'Mitochondria'
    and coalesce(t.order_name, '') <> 'Chloroplast'
    and r.sample_id is not null
),
per_genus as (
  select sample_id, ref_db_id, genus, sum(count)::bigint as count
  from kept
  group by sample_id, ref_db_id, genus
)
select g.sample_id, s.study_id, g.ref_db_id, g.genus, g.count,
       g.count::double precision / sum(g.count) over (partition by g.sample_id, g.ref_db_id) as rel_abundance
from per_genus g
join core.sample s on s.sample_id = g.sample_id;

create unique index genus_counts_pk on analysis.genus_counts (sample_id, ref_db_id, genus);
create index genus_counts_study on analysis.genus_counts (study_id);

comment on materialized view analysis.genus_counts is
  'Counts per sample and genus from the current pipeline runs; off-target taxa removed; refresh after each bio load.';
